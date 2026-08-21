import hashlib
import json
import math
import os
from fractions import Fraction

import numpy as np
import scipy.ndimage
import torch
from PIL import Image, ImageOps, ImageSequence

import comfy.utils
import comfy.sd
import comfy.model_management
from comfy.bg_removal_model import BackgroundRemovalModel
from comfy_api.latest import InputImpl, Types
import folder_paths
import node_helpers
from comfy_extras.nodes_sam3 import SAM3_Detect, SAM3_TrackToMask, SAM3_VideoTrack
from nodes import CheckpointLoaderSimple, CLIPTextEncode

try:
    from sam2.build_sam import build_sam2
    from sam2.sam2_image_predictor import SAM2ImagePredictor
    from sam2.utils.transforms import SAM2Transforms
    from torchvision.transforms import Normalize, Resize, ToTensor
except ImportError:
    build_sam2 = None
    SAM2ImagePredictor = None
    SAM2Transforms = None


RMBG_MODEL_PATH = os.path.join(folder_paths.models_dir, "RMBG", "RMBG-2.0", "model.safetensors")
RMBG_CONFIG_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), "rmbg_2_config.json")
SAM_MODEL_EXTENSIONS = getattr(folder_paths, "supported_pt_extensions", {".pt", ".pth", ".safetensors", ".ckpt"})


def _resolve_video_path(video, local_path=""):
    local_path = str(local_path or "").strip().strip('"')
    if local_path:
        path = os.path.normpath(os.path.expanduser(os.path.expandvars(local_path)))
        if not os.path.isabs(path):
            raise ValueError("Local video path must be an absolute path.")
        if not os.path.isfile(path):
            raise ValueError(f"Local video file not found: {path}")
        return path
    return folder_paths.get_annotated_filepath(video)


def _sam_model_choices():
    choices = [name for name in folder_paths.get_filename_list("checkpoints") if "sam3" in name.lower()]
    for family in ("sam3", "sam2"):
        model_dir = os.path.join(folder_paths.models_dir, family)
        if not os.path.isdir(model_dir):
            continue
        for name in sorted(os.listdir(model_dir)):
            path = os.path.join(model_dir, name)
            if os.path.isfile(path) and os.path.splitext(name)[1].lower() in SAM_MODEL_EXTENSIONS:
                choices.append(f"{family}/{name}")
    return choices or [""]


def _sam_model_path(name, family):
    prefix = f"{family}/"
    if name.startswith(prefix):
        filename = name[len(prefix):]
        if filename != os.path.basename(filename):
            raise ValueError(f"Invalid {family.upper()} model name: {name}")
        path = os.path.join(folder_paths.models_dir, family, filename)
        if os.path.isfile(path):
            return path
        raise FileNotFoundError(f"{family.upper()} model not found: {path}")
    if family == "sam3":
        return folder_paths.get_full_path_or_raise("checkpoints", name)
    raise ValueError(f"Select a model from models/{family} for a {family.upper()} slot.")


def _sam2_config_name(model_path):
    name = os.path.basename(model_path).lower()
    version = "sam2.1" if "sam2.1" in name else "sam2"
    if "large" in name or "hiera_l" in name:
        size = "l"
    elif "base_plus" in name or "hiera_b" in name:
        size = "b+"
    elif "small" in name or "hiera_s" in name:
        size = "s"
    elif "tiny" in name or "hiera_t" in name:
        size = "t"
    else:
        raise ValueError(f"Cannot determine SAM2 model size from: {os.path.basename(model_path)}")
    return f"configs/{version}/{version}_hiera_{size}.yaml"


def _load_sam2(model_path):
    if build_sam2 is None or SAM2ImagePredictor is None:
        raise RuntimeError("SAM2 support requires the sam2 Python package.")
    device = comfy.model_management.get_torch_device()
    model = build_sam2(_sam2_config_name(model_path), device="cpu")
    state_dict = comfy.utils.load_torch_file(model_path)
    if isinstance(state_dict.get("model"), dict):
        state_dict = state_dict["model"]
    model.load_state_dict(state_dict)
    model.to(device)
    # SAM2's scripted torchvision transform fails during construction on Python 3.13.
    predictor = SAM2ImagePredictor.__new__(SAM2ImagePredictor)
    predictor.model = model
    predictor._transforms = SAM2Transforms.__new__(SAM2Transforms)
    torch.nn.Module.__init__(predictor._transforms)
    predictor._transforms.resolution = model.image_size
    predictor._transforms.mask_threshold = 0.0
    predictor._transforms.max_hole_area = 0.0
    predictor._transforms.max_sprinkle_area = 0.0
    predictor._transforms.mean = [0.485, 0.456, 0.406]
    predictor._transforms.std = [0.229, 0.224, 0.225]
    predictor._transforms.to_tensor = ToTensor()
    predictor._transforms.transforms = torch.nn.Sequential(
        Resize((model.image_size, model.image_size)),
        Normalize(predictor._transforms.mean, predictor._transforms.std),
    )
    predictor._is_image_set = False
    predictor._features = None
    predictor._orig_hw = None
    predictor._is_batch = False
    predictor.mask_threshold = 0.0
    predictor._bb_feat_sizes = [(256, 256), (128, 128), (64, 64)]
    return predictor


def _load_sam3(model_path):
    state_dict = comfy.utils.load_torch_file(model_path)
    if isinstance(state_dict.get("model"), dict):
        state_dict = state_dict["model"]
    state_dict.pop("detector.backbone.language_backbone.encoder.text_projection", None)
    model, clip, _, _ = comfy.sd.load_state_dict_guess_config(
        state_dict,
        output_vae=False,
        output_clip=True,
        embedding_directory=folder_paths.get_folder_paths("embeddings"),
    )
    return model, clip


def _parse_rect(rect, width, height):
    if not isinstance(rect, dict):
        return None
    try:
        x = float(rect["x"])
        y = float(rect["y"])
        w = float(rect["w"])
        h = float(rect["h"])
    except (KeyError, TypeError, ValueError):
        return None
    x0 = max(0, min(width - 1, round(x * width)))
    y0 = max(0, min(height - 1, round(y * height)))
    x1 = max(x0 + 1, min(width, round((x + w) * width)))
    y1 = max(y0 + 1, min(height, round((y + h) * height)))
    return x0, y0, x1, y1


def _combine_masks(base, mask, operation):
    if base is None:
        return mask
    if operation == "subtract":
        return (base - mask).clamp(0.0, 1.0)
    if operation == "intersect":
        return torch.minimum(base, mask)
    if operation == "xor":
        return torch.logical_xor(base > 0.5, mask > 0.5).float()
    return torch.maximum(base, mask)


def _process_sam_mask(mask, grow, feather):
    grow = int(grow)
    feather = int(feather)
    if grow == 0 and feather == 0:
        return mask
    device = mask.device
    processed = []
    for frame in mask.reshape((-1, mask.shape[-2], mask.shape[-1])):
        binary = frame.detach().cpu().numpy() > 0.5
        if binary.any() and not binary.all():
            if grow > 0:
                binary |= scipy.ndimage.distance_transform_edt(~binary) <= grow
            elif grow < 0:
                binary &= scipy.ndimage.distance_transform_edt(binary) > -grow
        if feather > 0 and binary.any() and not binary.all():
            inside = scipy.ndimage.distance_transform_edt(binary)
            outside = scipy.ndimage.distance_transform_edt(~binary)
            output = np.clip(0.5 + (inside - outside) / (2.0 * feather), 0.0, 1.0).astype(np.float32)
        else:
            output = binary.astype(np.float32)
        processed.append(torch.from_numpy(output))
    return torch.stack(processed).to(device)


def _paint_masking_strokes(strokes, width, height):
    mask = np.zeros((height, width), dtype=np.float32)
    if not isinstance(strokes, list):
        return mask
    for stroke in strokes:
        if not isinstance(stroke, dict) or not isinstance(stroke.get("points"), list):
            continue
        try:
            radius = max(1, round(float(stroke.get("size", 24)) * 0.5))
        except (TypeError, ValueError):
            radius = 12
        value = 0.0 if stroke.get("mode") == "exclude" else 1.0
        for point in stroke["points"]:
            if not isinstance(point, dict):
                continue
            try:
                px = round(float(point["x"]) * width)
                py = round(float(point["y"]) * height)
            except (KeyError, TypeError, ValueError):
                continue
            x0 = max(0, px - radius)
            y0 = max(0, py - radius)
            x1 = min(width, px + radius + 1)
            y1 = min(height, py + radius + 1)
            if x1 <= x0 or y1 <= y0:
                continue
            yy, xx = np.ogrid[y0:y1, x0:x1]
            disk = (xx - px) ** 2 + (yy - py) ** 2 <= radius ** 2
            region = mask[y0:y1, x0:x1]
            region[disk] = value
    return mask


class JHLoadImageMask:
    CATEGORY = "JH/Image"
    FUNCTION = "load"
    RETURN_TYPES = ("IMAGE", "MASK", "IMAGE")
    RETURN_NAMES = ("image_with_alpha", "mask", "rgb_image")
    DESCRIPTION = "Load and interactively crop, mask, remove backgrounds, or segment an image with SAM2 or SAM3."

    @classmethod
    def INPUT_TYPES(cls):
        input_dir = folder_paths.get_input_directory()
        files = [name for name in os.listdir(input_dir) if os.path.isfile(os.path.join(input_dir, name))]
        files = folder_paths.filter_files_content_types(files, ["image"])
        sam_models = _sam_model_choices()
        return {
            "required": {
                "image": (sorted(files), {"image_upload": True}),
                "slot_config": ("STRING", {"default": "", "multiline": False}),
                "max_megapixels": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 128.0, "step": 0.01}),
                "selection_mode": (["REMOVE SELECTED", "KEEP SELECTED"],),
                "sam3_model": (sam_models,),
            }
        }

    def load(self, image, slot_config="", max_megapixels=0.0, selection_mode="REMOVE SELECTED", sam3_model=""):
        try:
            max_megapixels = float(max_megapixels)
        except (TypeError, ValueError):
            max_megapixels = 0.0
        if not math.isfinite(max_megapixels) or max_megapixels < 0:
            max_megapixels = 0.0
        image_path = folder_paths.get_annotated_filepath(image)
        pil_image = node_helpers.pillow(Image.open, image_path)
        images = []
        source_masks = []
        width = height = None

        for frame in ImageSequence.Iterator(pil_image):
            frame = node_helpers.pillow(ImageOps.exif_transpose, frame)
            rgb_frame = frame.convert("RGB")
            if width is None:
                width, height = rgb_frame.size
            if rgb_frame.size != (width, height):
                continue
            images.append(torch.from_numpy(np.array(rgb_frame).astype(np.float32) / 255.0).unsqueeze(0))
            if "A" in frame.getbands():
                alpha = torch.from_numpy(np.array(frame.getchannel("A")).astype(np.float32) / 255.0)
                source_masks.append((1.0 - alpha).unsqueeze(0))
            else:
                source_masks.append(torch.zeros((1, height, width), dtype=torch.float32))

        rgb = torch.cat(images, dim=0)
        source_mask = torch.cat(source_masks, dim=0)
        return self._process(rgb, source_mask, slot_config, max_megapixels, selection_mode, sam3_model)

    def _process(self, rgb, source_mask, slot_config, max_megapixels, selection_mode, sam3_model,
                 video_tracking=False, detect_interval=1, max_objects=4):
        height, width = rgb.shape[1:3]
        try:
            config = json.loads(slot_config) if slot_config else {}
        except (TypeError, json.JSONDecodeError):
            config = {}
        slots = config.get("slots", []) if isinstance(config, dict) else []

        crop_box = None
        for slot in slots:
            if not isinstance(slot, dict) or slot.get("enabled", True) is False or slot.get("mode", "crop") != "crop":
                continue
            box = _parse_rect(slot.get("rect"), width, height)
            if box is not None:
                crop_box = box if crop_box is None else (
                    min(crop_box[0], box[0]), min(crop_box[1], box[1]),
                    max(crop_box[2], box[2]), max(crop_box[3], box[3]),
                )

        if crop_box is not None:
            x0, y0, x1, y1 = crop_box
            rgb = rgb[:, y0:y1, x0:x1, :].clone()
            source_mask = source_mask[:, y0:y1, x0:x1].clone()

        combined_mask = None
        loaded_models = {}
        background_model = None
        background_foreground_mask = None
        for slot_index, slot in enumerate(slots, start=1):
            if not isinstance(slot, dict) or slot.get("enabled", True) is False:
                continue
            mode = slot.get("mode", "crop")
            if mode == "crop":
                continue

            if mode == "masking":
                if slot.get("masking_prompt_mode", "box") == "brush":
                    if combined_mask is None:
                        combined_mask = torch.zeros_like(source_mask)
                    strokes = slot.get("masking_brush_strokes", [])
                    if isinstance(strokes, list):
                        for stroke in strokes:
                            shape_stroke = {**stroke, "mode": "include"} if isinstance(stroke, dict) else stroke
                            painted = _paint_masking_strokes([shape_stroke], width, height)
                            if crop_box is not None:
                                crop_x0, crop_y0, crop_x1, crop_y1 = crop_box
                                painted = painted[crop_y0:crop_y1, crop_x0:crop_x1].copy()
                            stroke_mask = torch.from_numpy(painted).to(source_mask.device).unsqueeze(0).expand(rgb.shape[0], -1, -1)
                            operation = "subtract" if isinstance(stroke, dict) and stroke.get("mode") == "exclude" else "add"
                            combined_mask = _combine_masks(combined_mask, stroke_mask, operation)
                    continue
                else:
                    box = _parse_rect(slot.get("rect"), width, height)
                    if box is None:
                        continue
                    slot_mask = torch.zeros_like(source_mask)
                    x0, y0, x1, y1 = box
                    if crop_box is not None:
                        crop_x0, crop_y0, crop_x1, crop_y1 = crop_box
                        x0 = max(x0, crop_x0) - crop_x0
                        y0 = max(y0, crop_y0) - crop_y0
                        x1 = min(x1, crop_x1) - crop_x0
                        y1 = min(y1, crop_y1) - crop_y0
                        if x1 <= x0 or y1 <= y0:
                            continue
                    slot_mask[:, y0:y1, x0:x1] = 1.0
            elif mode == "sam3":
                prompt = str(slot.get("prompt", "")).strip()
                if not prompt:
                    raise ValueError(f"SAM3 slot {slot_index} requires an object prompt.")
                checkpoint = str(slot.get("model") or sam3_model)
                if checkpoint not in loaded_models:
                    checkpoint_path = _sam_model_path(checkpoint, "sam3")
                    if checkpoint.startswith("sam3/"):
                        model, clip = _load_sam3(checkpoint_path)
                    else:
                        model, clip, _ = CheckpointLoaderSimple().load_checkpoint(checkpoint)
                    loaded_models[checkpoint] = model, clip
                model, clip = loaded_models[checkpoint]
                conditioning = CLIPTextEncode().encode(clip, prompt)[0]
                if video_tracking:
                    track_data = SAM3_VideoTrack.execute(
                        images=rgb,
                        model=model,
                        conditioning=conditioning,
                        detection_threshold=float(slot.get("threshold", 0.5)),
                        max_objects=max_objects,
                        detect_interval=detect_interval,
                    )[0]
                    slot_mask = SAM3_TrackToMask.execute(track_data=track_data)[0]
                else:
                    result = SAM3_Detect.execute(
                        model=model,
                        image=rgb,
                        conditioning=conditioning,
                        threshold=float(slot.get("threshold", 0.5)),
                        refine_iterations=int(slot.get("refine_iterations", 2)),
                        individual_masks=bool(slot.get("individual_masks", False)),
                    )
                    slot_mask = result[0]
                    if slot_mask.shape[0] != rgb.shape[0]:
                        slot_mask = (slot_mask > 0.5).any(dim=0, keepdim=True).float().expand(rgb.shape[0], -1, -1)
                slot_mask = _process_sam_mask(slot_mask, slot.get("grow_mask", 0), slot.get("feather_mask", 0))
            elif mode == "sam2":
                prompt_mode = slot.get("sam2_prompt_mode", "box" if slot.get("rect") else "brush")
                box = _parse_rect(slot.get("rect"), width, height) if prompt_mode == "box" else None
                checkpoint = str(slot.get("model") or "")
                checkpoint_path = _sam_model_path(checkpoint, "sam2")
                if checkpoint not in loaded_models:
                    loaded_models[checkpoint] = _load_sam2(checkpoint_path)
                predictor = loaded_models[checkpoint]
                predict_box = None
                if box is not None:
                    x0, y0, x1, y1 = box
                    if crop_box is not None:
                        crop_x0, crop_y0, crop_x1, crop_y1 = crop_box
                        x0 = max(x0, crop_x0) - crop_x0
                        y0 = max(y0, crop_y0) - crop_y0
                        x1 = min(x1, crop_x1) - crop_x0
                        y1 = min(y1, crop_y1) - crop_y0
                        if x1 <= x0 or y1 <= y0:
                            continue
                    predict_box = np.array([x0, y0, x1, y1], dtype=np.float32)
                point_coords = []
                point_labels = []
                for field, label in (("sam2_positive_points", 1), ("sam2_negative_points", 0)):
                    points = slot.get(field, [])
                    if not isinstance(points, list):
                        continue
                    for point in points:
                        if not isinstance(point, dict):
                            continue
                        try:
                            px = float(point["x"]) * width
                            py = float(point["y"]) * height
                        except (KeyError, TypeError, ValueError):
                            continue
                        if crop_box is not None:
                            crop_x0, crop_y0, crop_x1, crop_y1 = crop_box
                            if not (crop_x0 <= px < crop_x1 and crop_y0 <= py < crop_y1):
                                continue
                            px -= crop_x0
                            py -= crop_y0
                        point_coords.append([px, py])
                        point_labels.append(label)
                if predict_box is None and 1 not in point_labels:
                    raise ValueError(f"SAM2 slot {slot_index} requires an INCLUDE brush stroke or a selection box.")
                masks = []
                for frame in rgb:
                    image = (frame[..., :3].detach().cpu().numpy() * 255.0).clip(0, 255).astype(np.uint8)
                    predictor.set_image(image)
                    predict_args = {"multimask_output": False}
                    if predict_box is not None:
                        predict_args["box"] = predict_box
                    if point_coords:
                        predict_args["point_coords"] = np.asarray(point_coords, dtype=np.float32)
                        predict_args["point_labels"] = np.asarray(point_labels, dtype=np.int32)
                    predicted, _, _ = predictor.predict(
                        **predict_args,
                    )
                    masks.append(torch.from_numpy(predicted[0].astype(np.float32)))
                slot_mask = torch.stack(masks).to(source_mask.device)
                slot_mask = _process_sam_mask(slot_mask, slot.get("grow_mask", 0), slot.get("feather_mask", 0))
            elif mode == "background":
                if video_tracking:
                    raise ValueError("BACKGROUND is not available for video because frame-by-frame removal is not temporally consistent.")
                if background_model is None:
                    if not os.path.isfile(RMBG_MODEL_PATH):
                        raise FileNotFoundError(f"RMBG-2.0 model not found: {RMBG_MODEL_PATH}")
                    background_model = BackgroundRemovalModel(RMBG_CONFIG_PATH)
                    state_dict = comfy.utils.load_torch_file(RMBG_MODEL_PATH)
                    background_model.load_sd(state_dict)
                    del state_dict
                if background_foreground_mask is None:
                    background_foreground_mask = background_model.encode_image(rgb)
                foreground_mask = _process_sam_mask(
                    background_foreground_mask,
                    slot.get("grow_mask", 0),
                    slot.get("feather_mask", 0),
                )
                slot_mask = 1.0 - foreground_mask
            else:
                continue
            combined_mask = _combine_masks(combined_mask, slot_mask, slot.get("operation", "add"))

        if combined_mask is None:
            final_mask = source_mask
        elif selection_mode == "KEEP SELECTED":
            final_mask = 1.0 - ((1.0 - source_mask) * combined_mask)
        else:
            final_mask = torch.maximum(source_mask, combined_mask)

        if max_megapixels > 0:
            height, width = rgb.shape[1:3]
            target = max_megapixels * 1024 * 1024
            if width * height > target:
                scale = (target / (width * height)) ** 0.5
                new_width = max(1, round(width * scale))
                new_height = max(1, round(height * scale))
                rgb = comfy.utils.common_upscale(rgb.movedim(-1, 1), new_width, new_height, "lanczos", "disabled").movedim(1, -1)
                final_mask = comfy.utils.common_upscale(final_mask.unsqueeze(1), new_width, new_height, "bilinear", "disabled").squeeze(1)

        alpha = (1.0 - final_mask).unsqueeze(-1)
        masked_rgb = rgb * alpha
        rgba = torch.cat((masked_rgb, alpha), dim=-1)
        return rgba, final_mask, rgb

    @classmethod
    def IS_CHANGED(cls, image, slot_config="", max_megapixels=0.0, selection_mode="REMOVE SELECTED", sam3_model=""):
        image_path = folder_paths.get_annotated_filepath(image)
        digest = hashlib.sha256()
        with open(image_path, "rb") as image_file:
            digest.update(image_file.read())
        digest.update(slot_config.encode("utf-8"))
        digest.update(str(max_megapixels).encode("ascii"))
        digest.update(selection_mode.encode("utf-8"))
        digest.update(sam3_model.encode("utf-8"))
        return digest.hexdigest()

    @classmethod
    def VALIDATE_INPUTS(cls, image, slot_config="", max_megapixels=0.0, selection_mode="REMOVE SELECTED", sam3_model=""):
        if not folder_paths.exists_annotated_filepath(image):
            return f"Invalid image file: {image}"
        return True


class JHLoadVideoMask(JHLoadImageMask):
    RETURN_TYPES = ("VIDEO", "MASK", "VIDEO", "IMAGE", "IMAGE", "AUDIO", "VHS_VIDEOINFO")
    RETURN_NAMES = ("masked_video", "mask", "rgb_video", "masked_images", "rgb_images", "audio", "video_info")
    DESCRIPTION = "Load and crop or mask a video, with temporally consistent SAM3 video tracking."

    @classmethod
    def INPUT_TYPES(cls):
        input_dir = folder_paths.get_input_directory()
        files = [name for name in os.listdir(input_dir) if os.path.isfile(os.path.join(input_dir, name))]
        files = folder_paths.filter_files_content_types(files, ["video"])
        checkpoints = folder_paths.get_filename_list("checkpoints")
        checkpoints.sort(key=lambda name: ("sam3" not in name.lower(), name.lower()))
        return {
            "required": {
                "video": (sorted(files) or [""], {"video_upload": True}),
                "slot_config": ("STRING", {"default": "", "multiline": False}),
                "max_megapixels": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 128.0, "step": 0.01}),
                "selection_mode": (["REMOVE SELECTED", "KEEP SELECTED"],),
                "sam3_model": (checkpoints,),
                "force_rate": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 60.0, "step": 1.0}),
                "frame_load_cap": ("INT", {"default": 0, "min": 0, "max": 100000, "step": 1}),
                "skip_first_frames": ("INT", {"default": 0, "min": 0, "max": 100000, "step": 1}),
                "detect_interval": ("INT", {"default": 1, "min": 1, "max": 120, "step": 1}),
                "max_objects": ("INT", {"default": 4, "min": 1, "max": 64, "step": 1}),
            },
            "optional": {
                # Optional and last so workflows/API prompts created before this
                # option was added remain valid and keep their widget positions.
                "local_path": ("STRING", {"default": "", "multiline": False}),
            }
        }

    def load(self, video, local_path="", slot_config="", max_megapixels=0.0, selection_mode="REMOVE SELECTED", sam3_model="",
             force_rate=0.0, frame_load_cap=0, skip_first_frames=0, detect_interval=1, max_objects=4):
        try:
            max_megapixels = float(max_megapixels)
        except (TypeError, ValueError):
            max_megapixels = 0.0
        if not math.isfinite(max_megapixels) or max_megapixels < 0:
            max_megapixels = 0.0

        video_path = _resolve_video_path(video, local_path)
        source_video = InputImpl.VideoFromFile(video_path)
        source_rate = source_video.get_frame_rate()
        source_frame_count = source_video.get_frame_count()
        source_duration = source_video.get_duration()
        source_width, source_height = source_video.get_dimensions()
        try:
            force_rate = float(force_rate)
        except (TypeError, ValueError):
            force_rate = 0.0
        if not math.isfinite(force_rate) or force_rate < 0:
            force_rate = 0.0
        target_rate = Fraction(str(force_rate)) if force_rate > 0 else source_rate
        frame_load_cap = max(0, int(frame_load_cap))
        skip_first_frames = max(0, int(skip_first_frames))
        start_time = skip_first_frames / float(target_rate)
        duration = frame_load_cap / float(target_rate) if frame_load_cap > 0 else 0.0
        loaded_video = source_video
        if start_time > 0 or duration > 0:
            loaded_video = source_video.as_trimmed(start_time, duration, strict_duration=False)
            if loaded_video is None:
                raise ValueError("skip_first_frames is beyond the end of the video.")
        components = loaded_video.get_components()
        if components.images.shape[0] == 0:
            raise ValueError("Video contains no decodable frames.")

        images = components.images
        alpha = components.alpha
        if target_rate != source_rate:
            output_count = max(1, round(images.shape[0] * float(target_rate) / float(source_rate)))
            if frame_load_cap > 0:
                output_count = min(output_count, frame_load_cap)
            indices = (torch.arange(output_count, dtype=torch.float64) * float(source_rate) / float(target_rate)).round().long().clamp_(max=images.shape[0] - 1)
            images = images.index_select(0, indices)
            if alpha is not None:
                alpha = alpha.index_select(0, indices)
        elif frame_load_cap > 0:
            images = images[:frame_load_cap]
            if alpha is not None:
                alpha = alpha[:frame_load_cap]

        audio = components.audio
        if audio is not None:
            sample_rate = int(audio["sample_rate"])
            sample_count = math.ceil(images.shape[0] / float(target_rate) * sample_rate)
            audio = {"waveform": audio["waveform"][..., :sample_count], "sample_rate": sample_rate}

        rgb = images[..., :3]
        if alpha is not None:
            source_mask = 1.0 - alpha[..., 0]
        else:
            source_mask = torch.zeros(rgb.shape[:3], dtype=rgb.dtype, device=rgb.device)
        try:
            config = json.loads(slot_config) if slot_config else {}
        except (TypeError, json.JSONDecodeError):
            config = {}
        slots = config.get("slots", []) if isinstance(config, dict) else []
        has_processing = max_megapixels > 0
        for slot in slots:
            if has_processing or not isinstance(slot, dict) or slot.get("enabled", True) is False:
                continue
            mode = slot.get("mode", "crop")
            if mode == "crop":
                has_processing = _parse_rect(slot.get("rect"), rgb.shape[2], rgb.shape[1]) is not None
            elif mode == "masking":
                if slot.get("masking_prompt_mode", "box") == "brush":
                    has_processing = bool(slot.get("masking_brush_strokes"))
                else:
                    has_processing = _parse_rect(slot.get("rect"), rgb.shape[2], rgb.shape[1]) is not None
            elif mode in ("sam3", "sam2", "background"):
                has_processing = True
        if has_processing:
            _, final_mask, processed_rgb = self._process(
                rgb, source_mask, slot_config, max_megapixels, selection_mode, sam3_model,
                video_tracking=True, detect_interval=int(detect_interval), max_objects=int(max_objects),
            )
            masked_rgb = processed_rgb * (1.0 - final_mask).unsqueeze(-1)
        else:
            final_mask = source_mask
            processed_rgb = rgb
            masked_rgb = rgb if alpha is None else rgb * alpha
        common = {"audio": audio, "frame_rate": target_rate, "metadata": components.metadata}
        if not has_processing and alpha is None and target_rate == source_rate:
            masked_video = loaded_video
            rgb_video = loaded_video
        else:
            masked_video = InputImpl.VideoFromComponents(Types.VideoComponents(images=masked_rgb, **common), bit_depth=source_video.get_bit_depth())
            rgb_video = InputImpl.VideoFromComponents(Types.VideoComponents(images=processed_rgb, **common), bit_depth=source_video.get_bit_depth())
        video_info = {
            "source_fps": float(source_rate),
            "source_frame_count": source_frame_count,
            "source_duration": source_duration,
            "source_width": source_width,
            "source_height": source_height,
            "loaded_fps": float(target_rate),
            "loaded_frame_count": images.shape[0],
            "loaded_duration": images.shape[0] / float(target_rate),
            "loaded_width": processed_rgb.shape[2],
            "loaded_height": processed_rgb.shape[1],
        }
        return masked_video, final_mask, rgb_video, masked_rgb, processed_rgb, audio, video_info

    @classmethod
    def IS_CHANGED(cls, video, local_path="", slot_config="", max_megapixels=0.0, selection_mode="REMOVE SELECTED", sam3_model="",
                   force_rate=0.0, frame_load_cap=0, skip_first_frames=0, detect_interval=1, max_objects=4):
        video_path = _resolve_video_path(video, local_path)
        return f"{os.path.getmtime(video_path)}:{slot_config}:{max_megapixels}:{selection_mode}:{sam3_model}:{force_rate}:{frame_load_cap}:{skip_first_frames}:{detect_interval}:{max_objects}"

    @classmethod
    def VALIDATE_INPUTS(cls, video, local_path="", **kwargs):
        if str(local_path or "").strip():
            try:
                _resolve_video_path(video, local_path)
            except (OSError, ValueError) as error:
                return str(error)
            return True
        if not folder_paths.exists_annotated_filepath(video):
            return f"Invalid video file: {video}"
        return True
