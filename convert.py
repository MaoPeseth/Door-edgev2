# convert_to_openvino.py
import os
import onnx
import openvino as ov

MODEL_DIR = os.path.expanduser(r"C:\Users\Mao Piseth\Downloads\buffalo_sc\buffalo_sc")

def inspect_and_convert(onnx_path, output_name, static_shape):
    print(f"\n--- {onnx_path} ---")
    model = onnx.load(onnx_path)
    for inp in model.graph.input:
        shape = [d.dim_value for d in inp.type.tensor_type.shape.dim]
        print(f"Input: {inp.name}, shape: {shape}")

    core = ov.Core()
    ov_model = core.read_model(onnx_path)

    # Reshape to static shape for CPU speed
    input_name = ov_model.inputs[0].get_any_name()
    ov_model.reshape({input_name: static_shape})

    ov.save_model(ov_model, f"{output_name}.xml", compress_to_fp16=True)
    print(f"Saved: {output_name}.xml / .bin")

if __name__ == "__main__":
    det_path = os.path.join(MODEL_DIR, "det_500m.onnx")
    rec_path = os.path.join(MODEL_DIR, "w600k_mbf.onnx")

    inspect_and_convert(det_path, os.path.join(MODEL_DIR, "det_500m_ov"), [1, 3, 320, 320])
    inspect_and_convert(rec_path, os.path.join(MODEL_DIR, "rec_mbf_ov"), [1, 3, 112, 112])