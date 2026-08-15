"""
core/insightface_ov.py

Native OpenVINO inference for the buffalo_sc pair you converted with convert.py:
    det_500m_ov.xml/.bin   (SCRFD detector, reshaped to 640x640)
    rec_mbf_ov.xml/.bin    (MobileFaceNet recognizer, reshaped to 112x112)

insightface's own `FaceAnalysis` only knows how to load .onnx files through
onnxruntime — it cannot open raw OpenVINO IR (.xml/.bin). This module talks to
openvino.runtime directly and re-implements the same pre/post-processing
insightface uses internally (SCRFD anchor decode + ArcFace 5-point alignment),
so the output (bbox, kps, embedding) is numerically equivalent to what
`insightface.app.FaceAnalysis(name="buffalo_sc").get(frame)` would give you.

Exposes a drop-in-ish `FaceAnalysisOV` with `.prepare()` and `.get(frame)`,
returning `Face` objects with `.bbox` and `.embedding` — the two attributes
Door-Edge's camera_worker.py actually uses.
"""
import os

import cv2
import numpy as np
import openvino as ov


# ---------------------------------------------------------------------------
# ArcFace 112x112 reference landmarks (standard insightface template)
# ---------------------------------------------------------------------------
_ARCFACE_DST = np.array([
    [38.2946, 51.6963],
    [73.5318, 51.5014],
    [56.0252, 71.7366],
    [41.5493, 92.3655],
    [70.7299, 92.2041]], dtype=np.float32)


def _umeyama(src, dst, estimate_scale=True):
    """Similarity transform (rotation+scale+translation) mapping src -> dst.
    Same result as skimage.transform.SimilarityTransform().estimate(src, dst),
    which is what insightface uses for face alignment — reimplemented here in
    plain numpy so we don't need scikit-image as an extra dependency."""
    num, dim = src.shape
    src_mean = src.mean(axis=0)
    dst_mean = dst.mean(axis=0)
    src_demean = src - src_mean
    dst_demean = dst - dst_mean

    A = dst_demean.T @ src_demean / num
    d = np.ones((dim,), dtype=np.float64)
    if np.linalg.det(A) < 0:
        d[dim - 1] = -1

    T = np.eye(dim + 1, dtype=np.float64)
    U, S, Vt = np.linalg.svd(A)
    rank = np.linalg.matrix_rank(A)

    if rank == 0:
        return np.nan * T
    elif rank == dim - 1:
        if np.linalg.det(U) * np.linalg.det(Vt) > 0:
            T[:dim, :dim] = U @ Vt
        else:
            s = d[dim - 1]
            d[dim - 1] = -1
            T[:dim, :dim] = U @ np.diag(d) @ Vt
            d[dim - 1] = s
    else:
        T[:dim, :dim] = U @ np.diag(d) @ Vt

    if estimate_scale:
        scale = 1.0 / src_demean.var(axis=0).sum() * (S @ d)
    else:
        scale = 1.0

    T[:dim, dim] = dst_mean - scale * (T[:dim, :dim] @ src_mean.T)
    T[:dim, :dim] *= scale
    return T


def norm_crop(img, landmark, image_size=112):
    """Warp a face to a canonical 112x112 crop using its 5-point landmarks."""
    M = _umeyama(landmark.astype(np.float64), _ARCFACE_DST.astype(np.float64), True)[:2, :]
    return cv2.warpAffine(img, M, (image_size, image_size), borderValue=0.0)


def _distance2bbox(points, distance):
    x1 = points[:, 0] - distance[:, 0]
    y1 = points[:, 1] - distance[:, 1]
    x2 = points[:, 0] + distance[:, 2]
    y2 = points[:, 1] + distance[:, 3]
    return np.stack([x1, y1, x2, y2], axis=-1)


def _distance2kps(points, distance):
    preds = []
    for i in range(0, distance.shape[1], 2):
        px = points[:, i % 2] + distance[:, i]
        py = points[:, i % 2 + 1] + distance[:, i + 1]
        preds.append(px)
        preds.append(py)
    return np.stack(preds, axis=-1)


def _nms(dets, thresh=0.4):
    x1, y1, x2, y2, scores = dets[:, 0], dets[:, 1], dets[:, 2], dets[:, 3], dets[:, 4]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1)
        h = np.maximum(0.0, yy2 - yy1)
        inter = w * h
        ovr = inter / (areas[i] + areas[order[1:]] - inter)
        inds = np.where(ovr <= thresh)[0]
        order = order[inds + 1]
    return keep


class Face:
    """Mimics the subset of insightface.app.common.Face that Door-Edge reads."""
    __slots__ = ("bbox", "kps", "det_score", "embedding")

    def __init__(self, bbox, kps, det_score, embedding):
        self.bbox = bbox            # np.ndarray [x1,y1,x2,y2]
        self.kps = kps              # np.ndarray [5,2] or None
        self.det_score = det_score  # float
        self.embedding = embedding  # np.ndarray [512] or None


class SCRFDOpenVINO:
    """SCRFD (det_500m) detector running on native OpenVINO IR."""

    def __init__(self, xml_path, device="CPU"):
        core = ov.Core()
        model = core.read_model(xml_path)
        self.compiled = core.compile_model(model, device)
        self.outputs = list(self.compiled.outputs)

        in_shape = self.compiled.inputs[0].get_shape()  # NCHW
        self.input_size = (int(in_shape[3]), int(in_shape[2]))  # (W, H)
        self.input_mean = 127.5
        self.input_std = 128.0
        self._feat_stride_fpn = [8, 16, 32]
        self._num_anchors = 2
        self.center_cache = {}
        self.nms_thresh = 0.4

    def _forward(self, img, thresh):
        blob = cv2.dnn.blobFromImage(
            img, 1.0 / self.input_std, self.input_size,
            (self.input_mean, self.input_mean, self.input_mean), swapRB=True,
        )
        result = self.compiled(blob)

        # Classify each output by its last dimension: 1=score, 4=bbox, 10=kps.
        # This is robust to whatever output order the ONNX->OV conversion kept,
        # instead of assuming a fixed index like insightface's onnxruntime code does.
        scores_raw, bboxes_raw, kpss_raw = [], [], []
        for out in self.outputs:
            arr = result[out]
            arr = arr.reshape(-1, arr.shape[-1])
            c = arr.shape[-1]
            if c == 1:
                scores_raw.append(arr)
            elif c == 4:
                bboxes_raw.append(arr)
            elif c == 10:
                kpss_raw.append(arr)
        # Sort each group by anchor count descending -> stride 8, 16, 32
        scores_raw.sort(key=lambda a: -a.shape[0])
        bboxes_raw.sort(key=lambda a: -a.shape[0])
        kpss_raw.sort(key=lambda a: -a.shape[0])

        input_height, input_width = img.shape[0], img.shape[1]
        scores_list, bboxes_list, kpss_list = [], [], []

        for idx, stride in enumerate(self._feat_stride_fpn):
            scores = scores_raw[idx]
            bbox_preds = bboxes_raw[idx] * stride
            kps_preds = kpss_raw[idx] * stride if kpss_raw else None

            height = input_height // stride
            width = input_width // stride
            key = (height, width, stride)
            if key in self.center_cache:
                anchor_centers = self.center_cache[key]
            else:
                anchor_centers = np.stack(
                    np.mgrid[:height, :width][::-1], axis=-1
                ).astype(np.float32)
                anchor_centers = (anchor_centers * stride).reshape((-1, 2))
                if self._num_anchors > 1:
                    anchor_centers = np.stack(
                        [anchor_centers] * self._num_anchors, axis=1
                    ).reshape((-1, 2))
                if len(self.center_cache) < 100:
                    self.center_cache[key] = anchor_centers

            pos_inds = np.where(scores.ravel() >= thresh)[0]
            bboxes = _distance2bbox(anchor_centers, bbox_preds)
            scores_list.append(scores[pos_inds])
            bboxes_list.append(bboxes[pos_inds])
            if kps_preds is not None:
                kpss = _distance2kps(anchor_centers, kps_preds).reshape(-1, 5, 2)
                kpss_list.append(kpss[pos_inds])

        return scores_list, bboxes_list, kpss_list

    def detect(self, img, thresh=0.5):
        input_w, input_h = self.input_size
        im_ratio = float(img.shape[0]) / img.shape[1]
        model_ratio = float(input_h) / input_w
        if im_ratio > model_ratio:
            new_height = input_h
            new_width = int(new_height / im_ratio)
        else:
            new_width = input_w
            new_height = int(new_width * im_ratio)
        det_scale = float(new_height) / img.shape[0]
        resized_img = cv2.resize(img, (new_width, new_height))
        det_img = np.zeros((input_h, input_w, 3), dtype=np.uint8)
        det_img[:new_height, :new_width, :] = resized_img

        scores_list, bboxes_list, kpss_list = self._forward(det_img, thresh)
        if not scores_list or all(s.shape[0] == 0 for s in scores_list):
            return None, None

        scores = np.vstack(scores_list)
        bboxes = np.vstack(bboxes_list) / det_scale
        kpss = np.vstack(kpss_list) / det_scale if kpss_list else None

        order = scores.ravel().argsort()[::-1]
        pre_det = np.hstack((bboxes, scores)).astype(np.float32, copy=False)
        pre_det = pre_det[order, :]
        if kpss is not None:
            kpss = kpss[order, :, :]

        keep = _nms(pre_det, self.nms_thresh)
        det = pre_det[keep, :]
        kpss = kpss[keep, :, :] if kpss is not None else None
        return det, kpss


class ArcFaceOpenVINO:
    """MobileFaceNet (rec_mbf) recognizer running on native OpenVINO IR."""

    def __init__(self, xml_path, device="CPU"):
        core = ov.Core()
        model = core.read_model(xml_path)
        self.compiled = core.compile_model(model, device)
        self.output = self.compiled.outputs[0]
        in_shape = self.compiled.inputs[0].get_shape()
        self.input_size = (int(in_shape[3]), int(in_shape[2]))  # (W, H)
        self.input_mean = 127.5
        self.input_std = 127.5

    def get(self, img, kps):
        aligned = norm_crop(img, kps, image_size=self.input_size[0])
        blob = cv2.dnn.blobFromImage(
            aligned, 1.0 / self.input_std, self.input_size,
            (self.input_mean,) * 3, swapRB=True,
        )
        emb = self.compiled(blob)[self.output]
        return emb.flatten().astype(np.float32)


class FaceAnalysisOV:
    """Drop-in replacement for insightface.app.FaceAnalysis, backed by the
    native OpenVINO IR models in `model_dir` (det_500m_ov.* + rec_mbf_ov.*)."""

    def __init__(self, model_dir, device="CPU"):
        det_xml = os.path.join(model_dir, "det_500m_ov.xml")
        rec_xml = os.path.join(model_dir, "rec_mbf_ov.xml")
        if not os.path.exists(det_xml):
            raise FileNotFoundError(f"Detector XML not found: {det_xml}")
        if not os.path.exists(rec_xml):
            raise FileNotFoundError(f"Recognizer XML not found: {rec_xml}")
        self.detector = SCRFDOpenVINO(det_xml, device)
        self.recognizer = ArcFaceOpenVINO(rec_xml, device)
        self.det_thresh = 0.5
        print(f"[FaceAnalysisOV] detector input={self.detector.input_size} "
              f"recognizer input={self.recognizer.input_size} device={device}")

    def prepare(self, ctx_id=0, det_size=None, det_thresh=0.5):
        # det_size is accepted for call-site compatibility with insightface's
        # FaceAnalysis.prepare(), but the real input size is fixed by the
        # static reshape you baked into det_500m_ov.xml in convert.py.
        self.det_thresh = det_thresh

    def get(self, img):
        bboxes, kpss = self.detector.detect(img, thresh=self.det_thresh)
        faces = []
        if bboxes is None:
            return faces
        for i in range(bboxes.shape[0]):
            bbox = bboxes[i, :4]
            det_score = float(bboxes[i, 4])
            kps = kpss[i] if kpss is not None else None
            embedding = self.recognizer.get(img, kps) if kps is not None else None
            faces.append(Face(bbox=bbox, kps=kps, det_score=det_score, embedding=embedding))
        return faces
