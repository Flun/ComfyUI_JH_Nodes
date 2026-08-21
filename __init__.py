import os
import json
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageSequence
from PIL.PngImagePlugin import PngInfo
import datetime
import imageio
import shutil
import tempfile
import torch
import torchaudio
import subprocess
import imageio_ffmpeg
import math
import scipy.io.wavfile
import av
import folder_paths
import comfy.model_management
import glob
import random
import base64
import io
import urllib.error
import urllib.parse
import urllib.request
import threading
import hashlib
import secrets
from aiohttp import web
from comfy_api.latest import io as comfy_io
from comfy_execution.graph_utils import ExecutionBlocker
from server import PromptServer
from nodes import LoraLoader, PreviewImage
from .auto_image_feed import JHAutoImageFeed, JHBrowserSessionSetup, delete_auto_feed_preset, get_auto_feed_presets, stop_active_crawlers, translate_to_english, translate_to_korean
from .jh_load_image_mask import JHLoadImageMask, JHLoadVideoMask

WEB_DIRECTORY = "./web"
JH_CATEGORY = "JH"
JH_UTILS_CATEGORY = "JH/Utils"
JH_IMAGE_CATEGORY = "JH/Image"
JH_SAVED_TEXTS_FILE = os.path.join(folder_paths.get_user_directory(), "jh_saved_texts.json")
JH_SAVED_TEXTS_LOCK = threading.Lock()
JH_LLAMA_PROMPT_CACHE_FILE = os.path.join(folder_paths.get_user_directory(), "jh_llama_prompt_cache.json")
JH_LLAMA_PROMPT_CACHE_LOCK = threading.Lock()
JH_LLAMA_PROMPT_CACHE_LIMIT = 512
JH_NAS_PREVIEW_PATHS = {}
JH_NAS_PREVIEW_LOCK = threading.Lock()
JH_NAS_PREVIEW_LIMIT = 256
JH_LOCAL_VIDEO_PATHS = {}
JH_LOCAL_VIDEO_LOCK = threading.Lock()
JH_LOCAL_VIDEO_LIMIT = 128


def _register_nas_preview(path):
    token = secrets.token_urlsafe(24)
    with JH_NAS_PREVIEW_LOCK:
        JH_NAS_PREVIEW_PATHS[token] = os.path.abspath(path)
        while len(JH_NAS_PREVIEW_PATHS) > JH_NAS_PREVIEW_LIMIT:
            JH_NAS_PREVIEW_PATHS.pop(next(iter(JH_NAS_PREVIEW_PATHS)))
    return token


def _register_local_video(path):
    token = secrets.token_urlsafe(24)
    with JH_LOCAL_VIDEO_LOCK:
        JH_LOCAL_VIDEO_PATHS[token] = os.path.abspath(path)
        while len(JH_LOCAL_VIDEO_PATHS) > JH_LOCAL_VIDEO_LIMIT:
            JH_LOCAL_VIDEO_PATHS.pop(next(iter(JH_LOCAL_VIDEO_PATHS)))
    return token


def _replace_with_symlink(link_path, target_path):
    try:
        os.remove(link_path)
        os.symlink(os.path.abspath(target_path), link_path)
        return True
    except OSError as error:
        print(f"[NAS Saver] Output link unavailable; Assets preview disabled: {error}")
        return False


def _embed_video_metadata(path, prompt, extra_pnginfo):
    metadata = dict(extra_pnginfo or {})
    if prompt is not None:
        metadata["prompt"] = prompt
    if not metadata:
        return

    metadata_path = f"{path}.metadata.mp4"
    try:
        with av.open(path, mode="r") as source:
            with av.open(metadata_path, mode="w", options={"movflags": "use_metadata_tags+faststart"}) as output:
                output.metadata.update(source.metadata)
                for key, value in metadata.items():
                    output.metadata[key] = value if isinstance(value, str) else json.dumps(value)

                stream_map = {}
                for stream in source.streams:
                    if stream.type in {"video", "audio", "subtitle"} and stream.codec_context is not None:
                        stream_map[stream] = output.add_stream_from_template(template=stream, opaque=True)
                for packet in source.demux():
                    if packet.stream in stream_map and packet.dts is not None:
                        packet.stream = stream_map[packet.stream]
                        output.mux(packet)
        os.replace(metadata_path, path)
    finally:
        if os.path.exists(metadata_path):
            os.remove(metadata_path)


def _encode_nvenc_video(path, images, fps, codec, crf):
    """Encode with system FFmpeg because imageio's bundled binary has no NVENC."""
    ffmpeg_exe = shutil.which("ffmpeg")
    if not ffmpeg_exe:
        raise RuntimeError(
            f"{codec} requires a system FFmpeg build with NVIDIA NVENC support, but ffmpeg was not found in PATH."
        )

    frames = iter(images)
    try:
        first_frame = next(frames)
    except StopIteration as error:
        raise ValueError("Cannot encode a video with no frames.") from error

    def frame_array(image):
        frame = image.detach().mul(255).clamp(0, 255).to(device="cpu", dtype=torch.uint8).numpy()
        if frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError(f"NVENC expects RGB frames, received shape {frame.shape}.")
        return np.ascontiguousarray(frame)

    first_array = frame_array(first_frame)
    height, width = first_array.shape[:2]
    command = [
        ffmpeg_exe, "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-video_size", f"{width}x{height}", "-framerate", str(fps),
        "-i", "-", "-an", "-c:v", codec,
        "-pix_fmt", "yuv420p", "-cq:v", str(crf),
        "-preset", "p6", "-rc", "vbr",
    ]
    if codec == "hevc_nvenc":
        command.extend(["-tag:v", "hvc1"])
    command.extend(["-movflags", "+faststart", path])

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    write_error = None
    try:
        process.stdin.write(first_array.tobytes())
        for image in frames:
            frame = frame_array(image)
            if frame.shape != first_array.shape:
                raise ValueError(
                    f"All video frames must have the same shape; expected {first_array.shape}, received {frame.shape}."
                )
            process.stdin.write(frame.tobytes())
    except BrokenPipeError as error:
        write_error = error
    except Exception:
        process.kill()
        process.wait()
        process.stderr.close()
        raise
    finally:
        if process.stdin and not process.stdin.closed:
            try:
                process.stdin.close()
            except BrokenPipeError as error:
                write_error = error

    error_output = process.stderr.read().decode("utf-8", errors="replace").strip()
    process.stderr.close()
    return_code = process.wait()

    if write_error is not None or return_code != 0:
        detail = error_output or f"FFmpeg exited with status {return_code}."
        raise RuntimeError(f"{codec} encoding failed: {detail}")


def _load_llama_prompt_cache():
    if not os.path.isfile(JH_LLAMA_PROMPT_CACHE_FILE):
        return {}
    try:
        with open(JH_LLAMA_PROMPT_CACHE_FILE, "r", encoding="utf-8") as file:
            cache = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        print(f"[JH llama.cpp Prompt] Could not read prompt cache: {error}")
        return {}
    if not isinstance(cache, dict):
        return {}
    return {key: value for key, value in cache.items() if isinstance(key, str) and isinstance(value, str)}


def _cached_llama_prompt(cache_key):
    with JH_LLAMA_PROMPT_CACHE_LOCK:
        return _load_llama_prompt_cache().get(cache_key)


def _cache_llama_prompt(cache_key, text):
    with JH_LLAMA_PROMPT_CACHE_LOCK:
        cache = _load_llama_prompt_cache()
        cache.pop(cache_key, None)
        cache[cache_key] = text
        while len(cache) > JH_LLAMA_PROMPT_CACHE_LIMIT:
            cache.pop(next(iter(cache)))
        os.makedirs(os.path.dirname(JH_LLAMA_PROMPT_CACHE_FILE), exist_ok=True)
        temporary_path = f"{JH_LLAMA_PROMPT_CACHE_FILE}.tmp"
        with open(temporary_path, "w", encoding="utf-8") as file:
            json.dump(cache, file, ensure_ascii=False)
        os.replace(temporary_path, JH_LLAMA_PROMPT_CACHE_FILE)


def _normalize_loras(loras):
    if not isinstance(loras, list):
        raise ValueError("LoRA information has an invalid format")
    normalized = []
    for lora in loras:
        if not isinstance(lora, dict) or not isinstance(lora.get("name"), str):
            raise ValueError("LoRA information has an invalid format")
        normalized.append({
            "name": lora["name"],
            "strength_model": float(lora.get("strength_model", 1.0)),
            "strength_clip": float(lora.get("strength_clip", 1.0)),
        })
    return normalized


def _parse_lora_slots(value):
    if not value:
        return []
    try:
        slots = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as error:
        raise ValueError("Additional LoRA slots have an invalid format") from error
    if not isinstance(slots, list):
        raise ValueError("Additional LoRA slots have an invalid format")
    normalized = []
    for slot in slots:
        if not isinstance(slot, dict) or not isinstance(slot.get("name"), str):
            raise ValueError("Additional LoRA slots have an invalid format")
        normalized.append({
            "enabled": bool(slot.get("enabled", True)),
            "name": slot["name"],
            "strength_model": float(slot.get("strength_model", 1.0)),
            "strength_clip": float(slot.get("strength_clip", 1.0)),
        })
    return normalized


def _load_saved_texts():
    if not os.path.isfile(JH_SAVED_TEXTS_FILE):
        return []
    try:
        with open(JH_SAVED_TEXTS_FILE, "r", encoding="utf-8") as file:
            saved_items = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read saved texts: {error}") from error
    if not isinstance(saved_items, list):
        raise RuntimeError("Saved text library has an invalid format")
    normalized_items = []
    for item in saved_items:
        if isinstance(item, str):
            normalized_items.append({"text": item, "loras": []})
        elif isinstance(item, dict) and isinstance(item.get("text"), str) and isinstance(item.get("loras", []), list):
            try:
                loras = _normalize_loras(item.get("loras", []))
            except (TypeError, ValueError) as error:
                raise RuntimeError("Saved text library has an invalid format") from error
            normalized_items.append({"text": item["text"], "loras": loras})
        else:
            raise RuntimeError("Saved text library has an invalid format")
    return normalized_items


def _save_text(text, loras=None):
    text = str(text or "")
    if not text.strip():
        raise ValueError("Text is empty")
    loras = _normalize_loras(loras or [])
    item = {"text": text, "loras": loras}
    with JH_SAVED_TEXTS_LOCK:
        saved_items = _load_saved_texts()
        if item in saved_items:
            return False, len(saved_items)
        saved_items.append(item)
        os.makedirs(os.path.dirname(JH_SAVED_TEXTS_FILE), exist_ok=True)
        temporary_path = f"{JH_SAVED_TEXTS_FILE}.tmp"
        with open(temporary_path, "w", encoding="utf-8") as file:
            json.dump(saved_items, file, ensure_ascii=False, indent=2)
        os.replace(temporary_path, JH_SAVED_TEXTS_FILE)
        return True, len(saved_items)


def _metadata_json(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _clipboard_texts_from_workflow(workflow):
    if not isinstance(workflow, dict):
        return []
    node_lists = [workflow.get("nodes", [])]
    definitions = workflow.get("definitions")
    subgraphs = definitions.get("subgraphs", []) if isinstance(definitions, dict) else []
    node_lists.extend(subgraph.get("nodes", []) for subgraph in subgraphs if isinstance(subgraph, dict))
    outputs = []
    for nodes in node_lists:
        if not isinstance(nodes, list):
            continue
        for node in nodes:
            if not isinstance(node, dict) or node.get("type") not in {"JHClipboardText", "FluxClipboardText"}:
                continue
            widget_values = node.get("widgets_values")
            text = widget_values[0] if isinstance(widget_values, list) and widget_values else None
            if isinstance(text, str) and text.strip() and text not in outputs:
                outputs.append(text)
    return outputs


def _clipboard_output_from_media(path):
    suffix = os.path.splitext(path)[1].lower()
    metadata = {}
    if suffix in {".png", ".webp", ".gif", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}:
        with Image.open(path) as image:
            metadata = dict(image.info)
    elif suffix in {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}:
        with av.open(path, mode="r") as container:
            metadata = dict(container.metadata)
    else:
        raise ValueError("Unsupported image or video format")

    metadata = {str(key).lower(): value for key, value in metadata.items()}
    workflow = _metadata_json(metadata.get("workflow"))
    if workflow is None:
        comment = _metadata_json(metadata.get("comment") or metadata.get("description"))
        workflow = comment.get("workflow") if isinstance(comment, dict) else None
        workflow = _metadata_json(workflow)

    texts = _clipboard_texts_from_workflow(workflow)
    if not texts:
        raise ValueError("No JH Text Clipboard edit text was found in the file metadata")
    if len(texts) > 1:
        raise ValueError("Multiple JH Text Clipboard edit texts were found in the file metadata")
    return texts[0]


@PromptServer.instance.routes.post("/jh/saved-texts")
async def save_jh_text(request):
    try:
        data = await request.json()
        added, count = _save_text(data.get("text"), data.get("loras"))
        return web.json_response({"added": added, "count": count})
    except ValueError as error:
        return web.json_response({"error": str(error)}, status=400)
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        return web.json_response({"error": str(error)}, status=500)


@PromptServer.instance.routes.post("/jh/clipboard-text/media-prompt")
async def get_jh_clipboard_media_prompt(request):
    temporary_path = None
    try:
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "media" or not field.filename:
            return web.json_response({"error": "No image or video file was provided"}, status=400)
        suffix = os.path.splitext(field.filename)[1].lower()
        if suffix not in {".png", ".webp", ".gif", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}:
            return web.json_response({"error": "Unsupported image or video format"}, status=400)
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temporary_file:
            temporary_path = temporary_file.name
            while chunk := await field.read_chunk():
                temporary_file.write(chunk)
        text = _clipboard_output_from_media(temporary_path)
        return web.json_response({"text": text})
    except (OSError, ValueError, av.error.FFmpegError) as error:
        return web.json_response({"error": str(error)}, status=400)
    finally:
        if temporary_path:
            try:
                os.remove(temporary_path)
            except OSError:
                pass


@PromptServer.instance.routes.get("/jh/loras")
async def get_jh_loras(request):
    loras, _, _ = folder_paths.get_filename_list_("loras")
    modified_times = {}
    for lora in loras:
        path = folder_paths.get_full_path("loras", lora)
        try:
            modified_times[lora] = os.path.getmtime(path) if path else 0
        except OSError:
            modified_times[lora] = 0
    loras.sort(key=modified_times.get, reverse=True)
    return web.json_response({"loras": loras})


@PromptServer.instance.routes.get("/jh/video-info")
async def get_jh_video_info(request):
    video = request.query.get("video", "")
    if not folder_paths.exists_annotated_filepath(video):
        return web.json_response({"error": "Invalid video file"}, status=400)
    video_path = folder_paths.get_annotated_filepath(video)
    try:
        with av.open(video_path, mode="r") as container:
            stream = container.streams.video[0]
            frame_rate = stream.average_rate or stream.guessed_rate or 1
            duration = float(stream.duration * stream.time_base) if stream.duration is not None else float(container.duration or 0) / av.time_base
            frame_count = int(stream.frames or round(duration * float(frame_rate)))
        return web.json_response({
            "fps": float(frame_rate),
            "frame_count": frame_count,
            "duration": duration,
        })
    except (av.error.FFmpegError, IndexError, OSError, ValueError) as error:
        return web.json_response({"error": str(error)}, status=400)


@PromptServer.instance.routes.post("/jh/local-video/resolve")
async def resolve_jh_local_video(request):
    try:
        data = await request.json()
        raw_path = str(data.get("path") or "").strip().strip('"')
        video_path = os.path.normpath(os.path.expanduser(os.path.expandvars(raw_path)))
        if not raw_path or not os.path.isabs(video_path):
            raise ValueError("Enter an absolute local video path.")
        if not os.path.isfile(video_path):
            raise ValueError(f"Video file not found: {video_path}")
        with av.open(video_path, mode="r") as container:
            stream = container.streams.video[0]
            frame_rate = stream.average_rate or stream.guessed_rate or 1
            duration = float(stream.duration * stream.time_base) if stream.duration is not None else float(container.duration or 0) / av.time_base
            frame_count = int(stream.frames or round(duration * float(frame_rate)))
        token = _register_local_video(video_path)
        return web.json_response({
            "token": token,
            "fps": float(frame_rate),
            "frame_count": frame_count,
            "duration": duration,
        })
    except (av.error.FFmpegError, IndexError, OSError, ValueError, json.JSONDecodeError) as error:
        return web.json_response({"error": str(error)}, status=400)


@PromptServer.instance.routes.get("/jh/local-video/view")
async def view_jh_local_video(request):
    token = request.query.get("token", "")
    with JH_LOCAL_VIDEO_LOCK:
        video_path = JH_LOCAL_VIDEO_PATHS.get(token)
    if not video_path or not os.path.isfile(video_path):
        raise web.HTTPNotFound()
    return web.FileResponse(video_path, headers={"Content-Disposition": "inline", "Cache-Control": "no-store"})


@PromptServer.instance.routes.get("/jh/nas-preview")
async def get_jh_nas_preview(request):
    token = request.query.get("token", "")
    with JH_NAS_PREVIEW_LOCK:
        media_path = JH_NAS_PREVIEW_PATHS.get(token)
    if not media_path or not os.path.isfile(media_path):
        raise web.HTTPNotFound()
    return web.FileResponse(media_path, headers={"Content-Disposition": "inline", "Cache-Control": "no-store"})


@PromptServer.instance.routes.post("/jh/auto-feed/stop")
async def stop_jh_auto_feed(request):
    comfy.model_management.interrupt_current_processing()
    try:
        stopped = stop_active_crawlers()
    except (OSError, subprocess.SubprocessError) as error:
        return web.json_response({"error": str(error)}, status=500)
    return web.json_response({"stopped": stopped})


@PromptServer.instance.routes.get("/jh/auto-feed/presets")
async def get_jh_auto_feed_presets(request):
    return web.json_response({"presets": get_auto_feed_presets()})


@PromptServer.instance.routes.post("/jh/auto-feed/presets/delete")
async def delete_jh_auto_feed_preset(request):
    try:
        data = await request.json()
    except (json.JSONDecodeError, web.HTTPBadRequest):
        return web.json_response({"error": "Invalid request"}, status=400)
    preset_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(preset_id, str) or not preset_id:
        return web.json_response({"error": "Preset id is required"}, status=400)
    return web.json_response({"deleted": delete_auto_feed_preset(preset_id)})

# ============================================================
# [신규] NAS 재시도 매니저 (Failover 로직 강화판)
# ============================================================
class NasRetryManager:
    def __init__(self):
        # ComfyUI Output 폴더에 대기열 파일 저장
        self.retry_file = os.path.join(folder_paths.get_output_directory(), "nas_retry_queue.json")

    def load_queue(self):
        if not os.path.exists(self.retry_file):
            return []
        try:
            with open(self.retry_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return []

    def save_queue(self, queue):
        try:
            with open(self.retry_file, 'w', encoding='utf-8') as f:
                json.dump(queue, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"[NAS Retry] Queue saving failed: {e}")

    def add_job(self, local_path, nas_path, delete_local_on_success=False, create_output_symlink_on_success=False):
        """실패한 작업을 큐에 추가"""
        queue = self.load_queue()
        # 이미 큐에 동일한 작업이 있는지 확인 (중복 방지)
        if not any(job['local'] == local_path and job['nas'] == nas_path for job in queue):
            queue.append({
                "local": local_path, 
                "nas": nas_path, 
                "delete_local_on_success": delete_local_on_success,
                "create_output_symlink_on_success": create_output_symlink_on_success,
                "timestamp": str(datetime.datetime.now())
            })
            self.save_queue(queue)
            print(f"[NAS Retry] Added to retry queue: {os.path.basename(local_path)}")

    def process_retries(self):
        """
        대기 중인 작업을 재시도합니다.
        시나리오 1: 로컬 파일이 없으면 -> 큐에서 제거 (수동 이동/삭제로 간주)
        시나리오 2: NAS 연결 실패 -> 큐에 유지 (다음 실행 때 재시도)
        """
        queue = self.load_queue()
        if not queue:
            return

        print(f"[NAS Retry] Processing {len(queue)} pending uploads...")
        remaining_queue = [] # 처리에 실패하여 다음으로 넘길 목록
        success_count = 0
        removed_count = 0

        for job in queue:
            local_p = job.get('local')
            nas_p = job.get('nas')
            
            # [시나리오 1 대응] 로컬 파일이 존재하는지 확인
            if not os.path.exists(local_p):
                # 파일이 없으면 사용자가 옮겼거나 지운 것이므로 큐에서 제거
                print(f"[NAS Retry] Source missing (Manual move/del?): {os.path.basename(local_p)} -> Removed from queue.")
                removed_count += 1
                continue 

            # [시나리오 2 대응] 재전송 시도
            try:
                # NAS 폴더 경로 확보
                nas_dir = os.path.dirname(nas_p)
                os.makedirs(nas_dir, exist_ok=True)
                
                # 파일 복사
                shutil.copy2(local_p, nas_p)
                print(f"[NAS Retry] Recovery Success: {os.path.basename(nas_p)}")
                success_count += 1
                if job.get("create_output_symlink_on_success"):
                    _replace_with_symlink(local_p, nas_p)
                elif job.get("delete_local_on_success"):
                    try:
                        os.remove(local_p)
                    except OSError as error:
                        print(f"[NAS Retry] Could not remove local copy: {error}")
                
                # 성공했으므로 remaining_queue에 추가하지 않음 (자동 제거 효과)

            except Exception as e:
                # [시나리오 2 대응] 여전히 실패한 경우
                # print(f"[NAS Retry] Retry Failed ({e}): {os.path.basename(local_p)}")
                # 실패했으므로 다음 번을 위해 리스트에 다시 담음
                remaining_queue.append(job)

        # 변경 사항이 있을 때만 파일 갱신
        if success_count > 0 or removed_count > 0:
            self.save_queue(remaining_queue)
            print(f"[NAS Retry] Done. Success: {success_count}, Removed: {removed_count}, Remaining: {len(remaining_queue)}")

# 전역 인스턴스
retry_manager = NasRetryManager()


def _image_tensor_to_pil(image):
    i = 255. * image.cpu().numpy()
    return Image.fromarray(np.clip(i, 0, 255).astype(np.uint8)).convert("RGB")


def _pil_to_image_tensor(pil_image):
    image = np.array(pil_image.convert("RGB")).astype(np.float32) / 255.0
    return torch.from_numpy(image)[None,]


def _empty_image_and_mask(width=512, height=512):
    image = torch.zeros((1, height, width, 3), dtype=torch.float32)
    mask = torch.ones((1, height, width), dtype=torch.float32)
    return image, mask


def _data_url_to_pil(data_url):
    if not data_url:
        return None
    text = str(data_url).strip()
    if "," in text and text.lower().startswith("data:image"):
        text = text.split(",", 1)[1]
    try:
        image_bytes = base64.b64decode(text)
        return Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as e:
        print(f"[JH Clipboard Image] Failed to decode image data: {e}")
        return None


def _clipboard_image_value_to_pil(image_value):
    if not image_value:
        return None

    text = str(image_value).strip()
    if text.lower().startswith("data:image"):
        return _data_url_to_pil(text)

    rel_path = os.path.normpath(text.replace("\\", os.sep).replace("/", os.sep))
    if os.path.isabs(rel_path) or rel_path.startswith(".."):
        print(f"[JH Clipboard Image] Refusing unsafe image path: {image_value}")
        return None

    input_dir = os.path.abspath(folder_paths.get_input_directory())
    full_path = os.path.abspath(os.path.join(input_dir, rel_path))
    try:
        if os.path.commonpath((input_dir, full_path)) != input_dir:
            print(f"[JH Clipboard Image] Refusing image path outside input: {image_value}")
            return None
    except ValueError:
        return None

    if not os.path.isfile(full_path):
        print(f"[JH Clipboard Image] Image file not found: {full_path}")
        return None

    try:
        return Image.open(full_path).convert("RGB")
    except Exception as e:
        print(f"[JH Clipboard Image] Failed to load image file: {e}")
        return None


def _download_civitai_json(url):
    headers = {"Accept": "application/json", "User-Agent": "ComfyUI-JH-Civitai-Image/1.0"}
    api_token = os.environ.get("CIVITAI_API_TOKEN")
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except (urllib.error.URLError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Civitai API request failed: {error}") from error


def _download_civitai_image(url):
    parsed_url = urllib.parse.urlparse(url)
    if parsed_url.scheme != "https" or parsed_url.hostname not in ("image.civitai.com", "imagecache.civitai.com"):
        raise RuntimeError("Civitai returned an unexpected image URL")

    request = urllib.request.Request(url, headers={"User-Agent": "ComfyUI-JH-Civitai-Image/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content_type = response.headers.get_content_type()
            if not content_type.startswith("image/"):
                raise RuntimeError(f"Civitai returned {content_type} instead of an image")
            image_bytes = response.read(50 * 1024 * 1024 + 1)
    except urllib.error.URLError as error:
        raise RuntimeError(f"Civitai image download failed: {error}") from error
    if len(image_bytes) > 50 * 1024 * 1024:
        raise RuntimeError("Civitai image exceeds the 50 MB download limit")
    try:
        return Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except (OSError, Image.DecompressionBombError) as error:
        raise RuntimeError(f"Civitai returned an invalid image: {error}") from error


class JHCivitaiImage:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "sort": (["Most Reactions", "Most Comments", "Newest"],),
                "period": (["AllTime", "Year", "Month", "Week", "Day"],),
                "mature_content": (["Exclude", "Only", "Include"],),
                "orientation": (["Any", "Portrait", "Landscape", "Square"],),
                "min_width": ("INT", {"default": 0, "min": 0, "max": 16384, "step": 64}),
                "min_height": ("INT", {"default": 0, "min": 0, "max": 16384, "step": 64}),
                "min_reactions": ("INT", {"default": 0, "min": 0, "max": 1000000}),
                "candidate_limit": ("INT", {"default": 100, "min": 1, "max": 200}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
            },
            "optional": {
                "model_id": ("STRING", {"default": ""}),
                "model_version_id": ("STRING", {"default": ""}),
                "username": ("STRING", {"default": ""}),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("image", "source_url", "page_url", "metadata_json")
    FUNCTION = "load_image"
    CATEGORY = JH_IMAGE_CATEGORY

    def load_image(self, sort, period, mature_content, orientation, min_width, min_height, min_reactions,
                   candidate_limit, seed, model_id="", model_version_id="", username=""):
        params = {"limit": candidate_limit, "sort": sort, "period": period}
        if mature_content != "Include":
            params["nsfw"] = "true" if mature_content == "Only" else "false"
        for name, value in (("modelId", model_id), ("modelVersionId", model_version_id)):
            value = str(value).strip()
            if value:
                if not value.isdigit():
                    raise ValueError(f"{name} must be a numeric Civitai ID")
                params[name] = value
        if username.strip():
            params["username"] = username.strip()

        api_url = "https://civitai.com/api/v1/images?" + urllib.parse.urlencode(params)
        data = _download_civitai_json(api_url)
        candidates = []
        for item in data.get("items", []):
            if item.get("type", "image") != "image":
                continue
            width = int(item.get("width") or 0)
            height = int(item.get("height") or 0)
            reactions = sum(int(value or 0) for name, value in item.get("stats", {}).items() if name != "commentCount")
            if width < min_width or height < min_height or reactions < min_reactions:
                continue
            if orientation == "Portrait" and width >= height:
                continue
            if orientation == "Landscape" and width <= height:
                continue
            if orientation == "Square" and width != height:
                continue
            candidates.append(item)
        if not candidates:
            raise RuntimeError("No Civitai images matched the filters. Increase candidate_limit or relax the filters.")

        item = candidates[seed % len(candidates)]
        image_url = item.get("url")
        if not image_url:
            raise RuntimeError("Civitai returned an image without a URL")
        image_id = item.get("id")
        page_url = f"https://civitai.com/images/{image_id}" if image_id is not None else ""
        metadata = json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str)
        return (_pil_to_image_tensor(_download_civitai_image(image_url)), image_url, page_url, metadata)


def _parse_rgb_color(color_text, default=(255, 255, 255)):
    if color_text is None:
        return default
    text = str(color_text).strip()
    if not text:
        return default

    named_colors = {
        "white": (255, 255, 255),
        "black": (0, 0, 0),
        "gray": (128, 128, 128),
        "grey": (128, 128, 128),
        "red": (255, 0, 0),
        "green": (0, 128, 0),
        "blue": (0, 0, 255),
    }
    if text.lower() in named_colors:
        return named_colors[text.lower()]

    if text.startswith("#"):
        hex_text = text[1:]
        if len(hex_text) == 3:
            hex_text = "".join(ch * 2 for ch in hex_text)
        if len(hex_text) == 6:
            try:
                return tuple(int(hex_text[i:i + 2], 16) for i in (0, 2, 4))
            except ValueError:
                return default

    try:
        parts = [int(part.strip()) for part in text.split(",")]
        if len(parts) == 3:
            return tuple(max(0, min(255, part)) for part in parts)
    except ValueError:
        pass

    return default


_JH_KOREAN_FONT_SUPPORT_CACHE = {}


def _font_supports_korean(font_path):
    supported = _JH_KOREAN_FONT_SUPPORT_CACHE.get(font_path)
    if supported is not None:
        return supported
    try:
        from fontTools.ttLib import TTFont
        with TTFont(font_path, fontNumber=0, lazy=True) as font:
            cmap = font.getBestCmap()
            supported = bool(cmap and 0xAC00 in cmap)
    except ImportError:
        supported = True
    except Exception:
        supported = False
    _JH_KOREAN_FONT_SUPPORT_CACHE[font_path] = supported
    return supported


def _load_font(font_size, font_path=""):
    try:
        if font_path and os.path.isfile(font_path):
            return ImageFont.truetype(font_path, font_size)
        windows_fonts = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")
        linux_fonts = [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJKkr-Regular.otf",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttf",
            "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
            "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        ]
        for candidate in [
            *linux_fonts,
            os.path.join(windows_fonts, "malgun.ttf"),
            os.path.join(windows_fonts, "malgunbd.ttf"),
            "malgun.ttf",
            "malgunbd.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]:
            try:
                font = ImageFont.truetype(candidate, font_size)
            except Exception:
                continue
            if _font_supports_korean(candidate):
                return font
    except Exception:
        pass
    return ImageFont.load_default()


def _wrap_caption_lines(draw, caption, font, max_width):
    lines = []
    for raw_line in str(caption or "").splitlines():
        if not raw_line:
            lines.append("")
            continue
        current = ""
        for word in raw_line.split(" "):
            candidate = word if current == "" else f"{current} {word}"
            bbox = draw.textbbox((0, 0), candidate, font=font)
            if bbox[2] - bbox[0] <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
                while len(current) > 1:
                    bbox = draw.textbbox((0, 0), current, font=font)
                    if bbox[2] - bbox[0] <= max_width:
                        break
                    split_at = len(current) - 1
                    while split_at > 1:
                        bbox = draw.textbbox((0, 0), current[:split_at], font=font)
                        if bbox[2] - bbox[0] <= max_width:
                            break
                        split_at -= 1
                    lines.append(current[:split_at])
                    current = current[split_at:]
        lines.append(current)
    return lines


def _draw_caption_in_area(draw, area, caption, font, fill, align="center"):
    left, top, right, bottom = area
    max_width = max(1, right - left)
    max_height = max(1, bottom - top)
    lines = _wrap_caption_lines(draw, caption, font, max_width)
    if not lines:
        return

    spacing = max(2, int(getattr(font, "size", 20) * 0.2))
    sizes = []
    total_height = spacing * max(0, len(lines) - 1)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        size = (bbox[2] - bbox[0], bbox[3] - bbox[1])
        sizes.append(size)
        total_height += size[1]

    y = top + max(0, (max_height - total_height) // 2)
    for line, (line_width, line_height) in zip(lines, sizes):
        if align == "left":
            x = left
        elif align == "right":
            x = right - line_width
        else:
            x = left + max(0, (max_width - line_width) // 2)
        draw.text((x, y), line, font=font, fill=fill)
        y += line_height + spacing


def _add_margin_and_caption(img, margin_position="bottom", margin_size=96, caption="", caption_font_size=36, caption_align="center", background_color="#ffffff", text_color="#000000", font_path=""):
    margin_size = max(0, int(margin_size))
    if margin_position == "none" or margin_size <= 0:
        return img

    top_margin = margin_size if margin_position in ("top", "both") else 0
    bottom_margin = margin_size if margin_position in ("bottom", "both") else 0
    background = _parse_rgb_color(background_color, (255, 255, 255))
    text_fill = _parse_rgb_color(text_color, (0, 0, 0))

    out = Image.new("RGB", (img.width, img.height + top_margin + bottom_margin), background)
    out.paste(img, (0, top_margin))

    if caption:
        draw = ImageDraw.Draw(out)
        font = _load_font(max(1, int(caption_font_size)), font_path)
        if top_margin > 0:
            area = (8, 0, img.width - 8, top_margin)
        else:
            area = (8, top_margin + img.height, img.width - 8, out.height)
        _draw_caption_in_area(draw, area, caption, font, text_fill, caption_align)

    return out


def _split_captions(captions, count):
    text = str(captions or "")
    if "||" in text:
        parts = [part.strip() for part in text.split("||")]
    else:
        parts = text.splitlines()
    while len(parts) < count:
        parts.append("")
    return parts[:count]


def _format_lora_info(lora_info):
    loras = _normalize_loras(lora_info or [])
    if not loras:
        return "None"
    formatted = []
    for lora in loras:
        name = lora["name"].replace("\\", "/")
        formatted.append(f"{name} (model {lora['strength_model']:g}, clip {lora['strength_clip']:g})")
    return ", ".join(formatted)


def _add_generation_info_panel(img, text="", translated_text="", lora_info=None, font_size=36, background_color="#ffffff", text_color="#000000", font_path=""):
    font = _load_font(max(1, int(font_size)), font_path)
    label_font = _load_font(max(1, int(font_size * 0.72)), font_path)
    padding = max(12, int(font_size * 0.6))
    label_gap = max(6, int(font_size * 0.3))
    section_gap = max(10, int(font_size * 0.55))
    max_width = max(1, img.width - padding * 2)
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    sections = [
        ("PROMPT", str(text or "").strip() or "None"),
    ]
    if translated_text:
        sections.append(("KOREAN", translated_text))
    sections.append(("LORA", _format_lora_info(lora_info)))
    measured = []
    panel_height = padding * 2
    for label, value in sections:
        lines = _wrap_caption_lines(measure, value, font, max_width)
        label_bbox = measure.textbbox((0, 0), label, font=label_font)
        line_height = max(1, measure.textbbox((0, 0), "Ag", font=font)[3])
        line_spacing = max(2, int(font_size * 0.2))
        text_height = len(lines) * line_height + max(0, len(lines) - 1) * line_spacing
        section_height = label_bbox[3] - label_bbox[1] + label_gap + text_height
        measured.append((label, lines, section_height, line_height, line_spacing))
        panel_height += section_height
    panel_height += section_gap * (len(measured) - 1)

    background = _parse_rgb_color(background_color, (255, 255, 255))
    text_fill = _parse_rgb_color(text_color, (0, 0, 0))
    out = Image.new("RGB", (img.width, img.height + panel_height), background)
    out.paste(img, (0, 0))
    draw = ImageDraw.Draw(out)
    divider = tuple((channel + text_channel) // 2 for channel, text_channel in zip(background, text_fill))
    draw.line((0, img.height, img.width, img.height), fill=divider, width=max(1, font_size // 12))

    y = img.height + padding
    for label, lines, section_height, line_height, line_spacing in measured:
        draw.text((padding, y), label, font=label_font, fill=divider)
        label_bbox = draw.textbbox((padding, y), label, font=label_font)
        text_y = y + (label_bbox[3] - label_bbox[1]) + label_gap
        for line in lines:
            draw.text((padding, text_y), line, font=font, fill=text_fill)
            text_y += line_height + line_spacing
        y += section_height + section_gap
    return out


# ============================================================
# 1. 이미지 저장 클래스
# ============================================================
class SaveImageToNAS:
    def __init__(self):
        self.output_dir = "output"
        self.type = "output"

    @classmethod
    def INPUT_TYPES(s):
        return {"required": 
                    {"images": ("IMAGE", ),
                     "directory": ("STRING", {"default": "C:\\Output", "multiline": False}),
                     "filename_prefix": ("STRING", {"default": "ComfyUI", "multiline": False}),
                     "use_date_folder": ("BOOLEAN", {"default": True}),
                     "format": (["png", "jpg", "webp"],),
                     "quality": ("INT", {"default": 100, "min": 1, "max": 100}),
                     },
                "optional": {
                     "save_metadata": ("BOOLEAN", {"default": True, "label_on": "On", "label_off": "Off"}),
                     "margin_position": (["none", "top", "bottom", "both"], {"default": "bottom"}),
                     "margin_size": ("INT", {"default": 96, "min": 0, "max": 4096, "step": 1}),
                     "caption": ("STRING", {"default": "", "multiline": True}),
                     "caption_font_size": ("INT", {"default": 36, "min": 1, "max": 512, "step": 1}),
                     "caption_align": (["center", "left", "right"], {"default": "center"}),
                     "background_color": ("STRING", {"default": "#ffffff", "multiline": False}),
                     "text_color": ("STRING", {"default": "#000000", "multiline": False}),
                     "font_path": ("STRING", {"default": "", "multiline": False}),
                     "use_caption_margin": ("BOOLEAN", {"default": False, "label_on": "Use Caption/Margin", "label_off": "No Caption/Margin"}),
                },
                "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
                }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("full_path",)
    FUNCTION = "save_images"
    OUTPUT_NODE = True
    CATEGORY = JH_CATEGORY

    def save_images(self, images, directory, filename_prefix, use_date_folder, format="png", quality=100, save_metadata=True, margin_position="bottom", margin_size=96, caption="", caption_font_size=36, caption_align="center", background_color="#ffffff", text_color="#000000", font_path="", use_caption_margin=False, prompt=None, extra_pnginfo=None):
        # [자동 재시도] 노드 실행 시마다 이전 실패 목록 확인
        retry_manager.process_retries()
        
        ui_results = list()
        path_results = list()

        # 1. 경로 설정
        if use_date_folder:
            today_str = datetime.datetime.now().strftime("%Y-%m-%d")
            nas_output_dir = os.path.join(directory, today_str)
            local_subfolder = today_str
        else:
            nas_output_dir = directory
            local_subfolder = ""

        local_output_root = folder_paths.get_output_directory()
        local_output_dir = os.path.join(local_output_root, local_subfolder)

        # 2. NAS 가용성 체크
        nas_available = False
        try:
            os.makedirs(nas_output_dir, exist_ok=True)
            nas_available = True
        except Exception as e:
            print(f"[NAS Saver] NAS Unreachable. Mode: Local Only.")
            nas_available = False
        
        os.makedirs(local_output_dir, exist_ok=True)

        # 카운터 계산 (NAS가 살아있으면 NAS 기준, 아니면 로컬 기준)
        target_counter_dir = nas_output_dir if nas_available else local_output_dir
        
        def get_counter(dir_path, prefix, ext):
            max_counter = 0
            try:
                for filename in os.listdir(dir_path):
                    if filename.startswith(prefix) and filename.endswith(f".{ext}"):
                        try:
                            rest = filename[len(prefix):]
                            if rest.startswith("_"): rest = rest[1:]
                            num_part = rest.split('.')[0]
                            count = int(num_part)
                            if count > max_counter: max_counter = count
                        except: pass
            except: pass
            return max_counter + 1

        counter = get_counter(target_counter_dir, filename_prefix, format)

        captions = _split_captions(caption, len(images))

        for idx, image in enumerate(images):
            img = _image_tensor_to_pil(image)
            if use_caption_margin:
                img = _add_margin_and_caption(
                    img,
                    margin_position=margin_position,
                    margin_size=margin_size,
                    caption=captions[idx],
                    caption_font_size=caption_font_size,
                    caption_align=caption_align,
                    background_color=background_color,
                    text_color=text_color,
                    font_path=font_path,
                )
            
            metadata = None
            if save_metadata and format == 'png':
                metadata = PngInfo()
                if prompt is not None:
                    metadata.add_text("prompt", json.dumps(prompt))
                if extra_pnginfo is not None:
                    for x in extra_pnginfo:
                        metadata.add_text(x, json.dumps(extra_pnginfo[x]))

            file_name = f"{filename_prefix}_{counter:05}.{format}"
            nas_full_path = os.path.join(nas_output_dir, file_name)
            local_full_path = os.path.join(local_output_dir, file_name)
            
            saved_to_nas = False
            nas_is_local_file = os.path.normcase(os.path.abspath(nas_full_path)) == os.path.normcase(os.path.abspath(local_full_path))
            
            # (1) 로컬 저장 (필수, 방어 코드)
            try:
                if format == 'png':
                    img.save(local_full_path, pnginfo=metadata, compress_level=4)
                else:
                    img.save(local_full_path, quality=quality)
            except Exception as e:
                print(f"[NAS Saver] CRITICAL: Local save failed! {e}")
                continue 
            
            # (2) NAS 복사 시도
            if nas_available:
                try:
                    if not nas_is_local_file:
                        shutil.copy2(local_full_path, nas_full_path)
                    print(f"[NAS Image] Saved: {nas_full_path}")
                    saved_to_nas = True
                except Exception as e:
                    print(f"[NAS Saver] NAS copy failed during run. Queueing retry.")
                    nas_available = False # 이후 이미지는 시도하지 않음 (시간 절약)
            
            # (3) 실패 시 큐 등록
            if not saved_to_nas:
                retry_manager.add_job(local_full_path, nas_full_path, delete_local_on_success=True)
                print(f"[NAS Saver] Saved LOCAL only: {local_full_path}")

            preview_path = nas_full_path if saved_to_nas else local_full_path
            if saved_to_nas and not nas_is_local_file:
                try:
                    os.remove(local_full_path)
                except OSError as error:
                    print(f"[NAS Image] Could not remove local copy: {error}")

            ui_results.append({
                "token": _register_nas_preview(preview_path),
                "filename": file_name,
                "format": format
            })
            path_results.append(preview_path)
            counter += 1

        return {"ui": {"jh_image_preview": ui_results}, "result": (path_results,)}

# ============================================================
# 2. 비디오 저장 클래스
# ============================================================
class SaveVideoToNAS:
    def __init__(self):
        self.type = "output"

    @classmethod
    def INPUT_TYPES(s):
        return {"required": 
                    {"directory": ("STRING", {"default": "C:\\Output_Video", "multiline": False}),
                     "filename_prefix": ("STRING", {"default": "ComfyVideo", "multiline": False}),
                     "use_date_folder": ("BOOLEAN", {"default": True}),
                     "fps": ("FLOAT", {"default": 24.0, "min": 0.01, "max": 1000.0, "step": 0.01}),
                     "format": (["mp4", "gif", "webp"],),
                     "codec": (["h264 (CPU)", "h265 (CPU - High Compression)", "hevc_nvenc (Nvidia GPU)", "h264_nvenc (Nvidia GPU)"],),
                     "crf": ("INT", {"default": 23, "min": 0, "max": 51, "tooltip": "낮을수록 고화질"}),
                     },
                "optional": {
                    "images": ("IMAGE", ),
                    "video": ("VIDEO", ),
                    "audio": ("AUDIO", ),
                    "save_metadata": ("BOOLEAN", {"default": True, "label_on": "On", "label_off": "Off"}),
                },
                "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
                }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("full_path",)
    FUNCTION = "save_video"
    OUTPUT_NODE = True
    CATEGORY = JH_CATEGORY

    def save_video(self, images=None, directory="C:\\Output_Video", filename_prefix="ComfyVideo", use_date_folder=True, fps=24.0, format="mp4", codec="h264 (CPU)", crf=23, audio=None, video=None, save_metadata=True, prompt=None, extra_pnginfo=None):
        if (images is None) == (video is None):
            raise ValueError("Connect exactly one of images or video")

        if video is not None:
            components = video.get_components()
            images = components.images
            fps = float(components.frame_rate)
            if audio is None:
                audio = components.audio

        # [자동 재시도] 노드 실행 시마다 이전 실패 목록 확인
        retry_manager.process_retries()
        
        ui_results = list()
        standard_ui_results = list()
        path_results = list()
        
        local_output_root = folder_paths.get_output_directory()
        
        # 1. 경로 설정
        if use_date_folder:
            today_str = datetime.datetime.now().strftime("%Y-%m-%d")
            nas_output_dir = os.path.join(directory, today_str)
            local_subfolder = today_str
        else:
            nas_output_dir = directory
            local_subfolder = ""

        local_final_dir = os.path.join(local_output_root, local_subfolder)

        # 2. NAS 체크
        nas_available = False
        try:
            os.makedirs(nas_output_dir, exist_ok=True)
            nas_available = True
        except:
            print(f"[NAS Video] NAS Unreachable. Mode: Local Only.")
            nas_available = False
            
        os.makedirs(local_final_dir, exist_ok=True)

        # 3. 카운터
        prefix_file = os.path.basename(filename_prefix)
        target_counter_dir = nas_output_dir if nas_available else local_final_dir
        
        def get_counter(dir_path, prefix, ext):
            max_counter = 0
            try:
                if os.path.exists(dir_path):
                    for filename in os.listdir(dir_path):
                        if filename.startswith(prefix) and filename.endswith(f".{ext}"):
                            try:
                                rest = filename[len(prefix):]
                                if rest.startswith("_"): rest = rest[1:]
                                num_part = rest.split('.')[0]
                                count = int(num_part)
                                if count > max_counter: max_counter = count
                            except: pass
            except: pass
            return max_counter + 1

        counter = get_counter(target_counter_dir, prefix_file, format)
        file_name = f"{filename_prefix}_{counter:05}.{format}"
        
        nas_full_path = os.path.join(nas_output_dir, file_name)
        local_full_path = os.path.join(local_final_dir, file_name)

        # 4. 인코딩 (Temp -> Local)
        safe_name = os.path.basename(file_name)
        temp_dir = tempfile.gettempdir()
        temp_video_path = os.path.join(temp_dir, f"temp_v_{safe_name}") 
        temp_audio_path = os.path.join(temp_dir, f"temp_a_{safe_name}.wav")
        temp_final_path = os.path.join(temp_dir, f"temp_final_{safe_name}")

        try:
            print(f"[NAS Video] Encoding...", flush=True)

            def frame_array(img):
                return img.detach().mul(255).clamp(0, 255).to(device="cpu", dtype=torch.uint8).numpy()
            
            # --- FFMPEG / ImageIO 인코딩 (기존 동일) ---
            if format == 'mp4':
                ffmpeg_params = []
                target_codec = 'libx264'
                if "h265" in codec:
                    target_codec = 'libx265'
                    ffmpeg_params = ['-crf', str(crf), '-preset', 'slow', '-tag:v', 'hvc1']
                elif "hevc_nvenc" in codec:
                    target_codec = 'hevc_nvenc'
                    ffmpeg_params = ['-cq', str(crf), '-preset', 'p6', '-tag:v', 'hvc1', '-rc', 'vbr']
                elif "h264_nvenc" in codec:
                    target_codec = 'h264_nvenc'
                    ffmpeg_params = ['-cq', str(crf), '-preset', 'p6', '-rc', 'vbr']
                else: 
                    target_codec = 'libx264'
                    ffmpeg_params = ['-crf', str(crf), '-preset', 'medium']

                if target_codec in {"h264_nvenc", "hevc_nvenc"}:
                    _encode_nvenc_video(temp_video_path, images, fps, target_codec, crf)
                else:
                    with imageio.get_writer(temp_video_path, fps=fps, codec=target_codec, pixelformat='yuv420p', ffmpeg_params=ffmpeg_params) as writer:
                        for img in images:
                            writer.append_data(frame_array(img))
            elif format == 'gif':
                imageio.mimsave(temp_final_path, [frame_array(img) for img in images], duration=(1000.0/fps), loop=0)
            elif format == 'webp':
                imageio.mimsave(temp_final_path, [frame_array(img) for img in images], fps=fps, quality=85)
            
            if format == 'mp4':
                has_audio = False
                if audio is not None:
                    try:
                        waveform = audio.get('waveform')
                        sample_rate = audio.get('sample_rate')
                        if waveform is not None:
                            if waveform.dim() == 3: waveform = waveform[0]
                            audio_np = waveform.cpu().numpy()
                            if audio_np.shape[0] < audio_np.shape[1]: audio_np = audio_np.T
                            scipy.io.wavfile.write(temp_audio_path, sample_rate, audio_np)
                            has_audio = True
                    except: pass

                ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
                base_cmd = [ffmpeg_exe, "-y", "-v", "error", "-i", temp_video_path]
                if has_audio: base_cmd += ["-i", temp_audio_path]

                encoding_cmd = ["-c:v", "copy"]
                if has_audio: encoding_cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]

                subprocess.run(base_cmd + encoding_cmd + [temp_final_path], check=True)
                if save_metadata:
                    _embed_video_metadata(temp_final_path, prompt, extra_pnginfo)

            # 5. 파일 이동 및 복사 (핵심 Failover 로직)
            source_file = temp_final_path
            saved_to_nas = False
            nas_is_local_file = os.path.normcase(os.path.abspath(nas_full_path)) == os.path.normcase(os.path.abspath(local_full_path))

            # (1) 로컬로 먼저 이동 (가장 중요)
            shutil.move(source_file, local_full_path)
            
            # (2) NAS 복사 시도
            if nas_available:
                try:
                    if not nas_is_local_file:
                        shutil.copy2(local_full_path, nas_full_path)
                    print(f"[NAS Video] Copy success: {nas_full_path}")
                    saved_to_nas = True
                except Exception as e:
                    print(f"[NAS Video] Copy failed: {e}")
                    saved_to_nas = False

            # (3) 실패 시 큐 등록
            if not saved_to_nas:
                retry_manager.add_job(local_full_path, nas_full_path, create_output_symlink_on_success=True)
                print(f"[NAS Video] Saved LOCAL only. Queueing retry.")

            preview_path = nas_full_path if saved_to_nas else local_full_path
            output_preview_available = not saved_to_nas or nas_is_local_file
            if saved_to_nas and not nas_is_local_file:
                output_preview_available = _replace_with_symlink(local_full_path, nas_full_path)

            if format == "mp4":
                ui_results.append({
                    "token": _register_nas_preview(preview_path),
                    "filename": file_name,
                    "format": format
                })
            elif output_preview_available:
                standard_ui_results.append({
                    "filename": file_name,
                    "subfolder": local_subfolder,
                    "type": "output"
                })
            else:
                ui_results.append({
                    "token": _register_nas_preview(preview_path),
                    "filename": file_name,
                    "format": format
                })
            path_results.append(preview_path)

        except Exception as e:
            print(f"[NAS Video] Error: {e}", flush=True)
            raise RuntimeError(f"JH Save Video to NAS failed: {e}") from e
        finally:
            for p in [temp_video_path, temp_audio_path, temp_final_path]:
                if os.path.exists(p):
                    try: os.remove(p)
                    except: pass

        ui = {}
        if standard_ui_results:
            ui.update({"images": standard_ui_results, "animated": (True,)})
        if ui_results:
            ui["jh_video_preview"] = ui_results
        return {"ui": ui, "result": (path_results,)}

# ============================================================
# 3. 크롭 좌표 계산 노드
# ============================================================
class CalculateCropWindow:
    def __init__(self): pass
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
                    "width": ("INT", {"default": 720, "min": 1, "max": 8192}),
                    "height": ("INT", {"default": 1280, "min": 1, "max": 8192}),
                    "zoom": ("FLOAT", {"default": 1.0, "min": 1.0, "max": 10.0, "step": 0.01}),
                    "position": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.01}), 
                    "x_align": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.01}),
                }}
    RETURN_TYPES = ("INT", "INT", "INT", "INT", "INT", "INT")
    RETURN_NAMES = ("crop_x", "crop_y", "crop_width", "crop_height", "input_width", "input_height")
    FUNCTION = "calculate_crop"
    CATEGORY = JH_UTILS_CATEGORY
    def calculate_crop(self, width, height, zoom, position, x_align=0.5):
        new_w = math.floor(width / zoom)
        new_h = math.floor(height / zoom)
        final_x = math.floor((width - new_w) * x_align)
        final_y = math.floor((height - new_h) * position)
        final_x = max(0, min(final_x, width - new_w))
        final_y = max(0, min(final_y, height - new_h))
        return (final_x, final_y, new_w, new_h, width, height)

# ============================================================
# 4. 해상도 비율 계산기 노드
# ============================================================
class AspectRatioCalculator:
    def __init__(self): pass
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "long_edge": ("INT", {"default": 1152, "min": 64, "max": 8192, "step": 8}),
                "aspect_ratio": (["9:16", "16:9", "1:1", "4:3", "3:4", "3:2", "2:3", "21:9", "9:21"], {"default": "9:16"}),
                "multiple_of": (["None", "2", "8", "16", "32", "64"], {"default": "8"}),
            }
        }
    RETURN_TYPES = ("INT", "INT")
    RETURN_NAMES = ("width", "height")
    FUNCTION = "calculate_resolution"
    CATEGORY = JH_UTILS_CATEGORY
    def calculate_resolution(self, long_edge, aspect_ratio, multiple_of="8"):
        try:
            if isinstance(aspect_ratio, list): aspect_ratio = aspect_ratio[0]
            w_str, h_str = aspect_ratio.split(":")
            w_ratio = float(w_str)
            h_ratio = float(h_str)
        except: w_ratio, h_ratio = 1.0, 1.0
        target_ratio = w_ratio / h_ratio
        if w_ratio >= h_ratio: raw_w = long_edge; raw_h = long_edge / target_ratio
        else: raw_h = long_edge; raw_w = long_edge * target_ratio
        width = int(round(raw_w))
        height = int(round(raw_h))
        if multiple_of != "None":
            m = int(multiple_of)
            width = int(round(raw_w / m) * m)
            height = int(round(raw_h / m) * m)
            if width < m: width = m
            if height < m: height = m
        return (width, height)

# ============================================================
# 5. [수정됨] 시작/끝 프레임 추출 노드 (+End Skip 기능 추가)
# ============================================================
class ResolutionDurationCalculator:
    ASPECT_RATIOS = {
        "1:1 (Square)": (1, 1),
        "2:3 (Portrait Photo)": (2, 3),
        "3:2 (Photo)": (3, 2),
        "3:4 (Portrait Standard)": (3, 4),
        "4:3 (Standard)": (4, 3),
        "9:16 (Portrait Widescreen)": (9, 16),
        "16:9 (Widescreen)": (16, 9),
        "21:9 (Ultrawide)": (21, 9),
    }

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "use_image_resolution": ("BOOLEAN", {"default": True}),
                "aspect_ratio": (list(cls.ASPECT_RATIOS), {"default": "3:4 (Portrait Standard)"}),
                "megapixels": ("FLOAT", {"default": 1.3, "min": 0.1, "max": 16.0, "step": 0.1}),
                "multiple": ("INT", {"default": 32, "min": 8, "max": 128, "step": 4}),
                "duration": ("INT", {"default": 5, "min": 1, "max": 3600, "step": 1}),
            },
            "optional": {
                "image": ("IMAGE",),
            },
        }

    RETURN_TYPES = ("INT", "INT", "INT")
    RETURN_NAMES = ("width", "height", "frame_count")
    FUNCTION = "calculate"
    CATEGORY = JH_UTILS_CATEGORY

    def calculate(self, use_image_resolution, aspect_ratio, megapixels, multiple, duration, image=None):
        if use_image_resolution:
            if image is None:
                raise ValueError("Connect an image or turn off use_image_resolution")
            height, width = image.shape[-3:-1]
            width_ratio, height_ratio = int(width), int(height)
        else:
            width_ratio, height_ratio = self.ASPECT_RATIOS[aspect_ratio]

        total_pixels = megapixels * 1024 * 1024
        scale = math.sqrt(total_pixels / (width_ratio * height_ratio))
        width = max(multiple, round(width_ratio * scale / multiple) * multiple)
        height = max(multiple, round(height_ratio * scale / multiple) * multiple)

        base_frames = max(5, round(duration * 24))
        frame_count = base_frames + (5 - (base_frames % 17)) % 17
        return (width, height, frame_count)


class ExtractStartEndFrames:
    def __init__(self): pass

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "images": ("IMAGE",),
                "start_skip_frames": ("INT", {"default": 0, "min": 0, "max": 10000, "step": 1, "tooltip": "앞에서 n번째 프레임을 추출합니다. (0=첫프레임)"}),
                "end_skip_frames": ("INT", {"default": 0, "min": 0, "max": 10000, "step": 1, "tooltip": "뒤에서 n번째 프레임을 추출합니다. (0=마지막프레임)"}),
            }
        }

    RETURN_TYPES = ("IMAGE", "IMAGE", "INT")
    RETURN_NAMES = ("start_frame", "end_frame", "total_count")
    FUNCTION = "extract_frames"
    CATEGORY = JH_UTILS_CATEGORY

    def extract_frames(self, images, start_skip_frames, end_skip_frames):
        # images.shape = [batch, height, width, channels]
        total_frames = images.shape[0]
        
        # 1. 시작 프레임 계산 (앞에서부터)
        # 예: 0이면 인덱스 0. 범위 넘어가면 마지막 프레임으로 제한
        start_idx = start_skip_frames
        if start_idx >= total_frames:
            start_idx = total_frames - 1
        
        # 2. 마지막 프레임 계산 (뒤에서부터)
        # 공식: (전체길이 - 1) - 스킵할 양
        # 예: 총 100장(0~99). skip 0 -> 99-0 = 99번(찐마지막)
        # 예: 총 100장(0~99). skip 1 -> 99-1 = 98번(마지막 바로 앞)
        end_idx = (total_frames - 1) - end_skip_frames
        
        # 범위가 0보다 작아지면(너무 많이 스킵하면) 첫 프레임(0)으로 고정
        if end_idx < 0:
            end_idx = 0

        # 3. 추출 (차원 유지)
        start_image = images[start_idx:start_idx+1]
        end_image = images[end_idx:end_idx+1]

        return (start_image, end_image, total_frames)

# ============================================================
# 6. 이미지 로드 노드 (클립보드/디렉토리 경로 지원)
# ============================================================
class LoadImageFromPath:
    """
    단일 이미지 입력 또는 디렉토리 경로에서 이미지를 로드합니다.
    - 클립보드 이미지 붙여넣기 지원 (단일 이미지 모드)
    - 디렉토리 경로 입력 시 해당 폴더의 이미지를 순차/순번/랜덤으로 로드
    - SMB 네트워크 경로 지원
    """
    
    def __init__(self):
        self.last_loaded_index = -1
        self.last_directory = ""
        self.image_files = []
        self.seed_counter = 0  # 랜덤 시드용 카운터
    
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "load_mode": (["single", "directory"], {
                    "default": "single",
                    "tooltip": "single: 단일 이미지/클립보드, directory: 디렉토리에서 로드"
                }),
                "image_path": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "단일 이미지 파일 경로 또는 디렉토리 경로"
                }),
            },
            "optional": {
                "directory_load_mode": (["sequential", "index", "random"], {
                    "default": "sequential",
                    "tooltip": "sequential: 순차적, index: 지정된 순번, random: 완전 랜덤"
                }),
                "image_index": ("INT", {
                    "default": 0,
                    "min": 0,
                    "max": 100000,
                    "step": 1,
                    "tooltip": "index 모드에서 사용할 이미지 순번 (0-based)"
                }),
                "seed": ("INT", {
                    "default": 0,
                    "min": 0,
                    "max": 0xffffffffffffffff,
                    "step": 1,
                    "tooltip": "random 모드의 시드값 (변경 시 새로운 랜덤 이미지)"
                }),
                "refresh": ("BOOLEAN", {
                    "default": False,
                    "label_on": "Refresh",
                    "label_off": "Refresh",
                    "tooltip": "디렉토리 파일 목록을 새로고침"
                }),
            }
        }
    
    RETURN_TYPES = ("IMAGE", "MASK", "STRING", "INT", "INT")
    RETURN_NAMES = ("image", "mask", "file_path", "current_index", "total_count")
    FUNCTION = "load_image"
    CATEGORY = JH_CATEGORY
    
    def load_image(self, load_mode="single", image_path="", 
                   directory_load_mode="sequential", image_index=0, seed=0, refresh=False):
        
        # 단일 이미지 모드
        if load_mode == "single":
            return self._load_single_image(image_path)
        
        # 디렉토리 모드
        else:
            return self._load_from_directory(
                image_path, 
                directory_load_mode, 
                image_index,
                seed,
                refresh
            )
    
    def _load_single_image(self, image_path):
        """단일 이미지 로드 (클립보드 또는 파일 경로)"""
        
        # 클립보드에서 이미지 시도 (Windows)
        if not image_path or image_path.strip() == "":
            try:
                from PIL import ImageGrab
                clipboard_img = ImageGrab.grabclipboard()
                if not clipboard_img and os.name != "nt" and shutil.which("xclip"):
                    try:
                        png = subprocess.run(
                            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
                            capture_output=True, timeout=5,
                        )
                        if png.returncode == 0 and png.stdout:
                            clipboard_img = Image.open(io.BytesIO(png.stdout))
                    except Exception:
                        clipboard_img = None
                if clipboard_img:
                    # 클립보드에 이미지가 있으면 사용
                    if isinstance(clipboard_img, Image.Image):
                        image = self._pil_to_tensor(clipboard_img.convert("RGB"))
                        mask = self._image_to_mask(image)
                        return (image, mask, "clipboard", 0, 1)
            except Exception as e:
                print(f"[LoadImage] Clipboard access failed: {e}")
        
        # 파일 경로에서 로드
        if image_path and os.path.isfile(image_path):
            try:
                img = Image.open(image_path)
                image = self._pil_to_tensor(img.convert("RGB"))
                mask = self._image_to_mask(image)
                return (image, mask, image_path, 0, 1)
            except Exception as e:
                print(f"[LoadImage] Failed to load image: {e}")
        
        # 둘 다 실패하면 빈 이미지 반환
        empty_image = torch.zeros((1, 512, 512, 3), dtype=torch.float32)
        empty_mask = torch.ones((1, 512, 512), dtype=torch.float32)
        return (empty_image, empty_mask, "", 0, 0)
    
    def _load_from_directory(self, directory, load_mode, image_index, seed=0, refresh=False):
        """디렉토리에서 이미지 로드"""
        
        if not directory or not os.path.isdir(directory):
            # 디렉토리가 없으면 빈 결과 반환
            empty_image = torch.zeros((1, 512, 512, 3), dtype=torch.float32)
            empty_mask = torch.ones((1, 512, 512), dtype=torch.float32)
            return (empty_image, empty_mask, "", 0, 0)
        
        # 파일 목록 새로고침 필요 시 (경로 변경 또는 refresh)
        if refresh or directory != self.last_directory:
            self.image_files = self._scan_image_files(directory)
            self.last_directory = directory
            self.last_loaded_index = -1
        
        if not self.image_files:
            empty_image = torch.zeros((1, 512, 512, 3), dtype=torch.float32)
            empty_mask = torch.ones((1, 512, 512), dtype=torch.float32)
            return (empty_image, empty_mask, "", 0, 0)
        
        total_count = len(self.image_files)
        selected_index = 0
        
        # 로드 모드별 인덱스 결정
        if load_mode == "sequential":
            # 순차적 로드 (마지막 인덱스 + 1)
            self.last_loaded_index = (self.last_loaded_index + 1) % total_count
            selected_index = self.last_loaded_index
        elif load_mode == "index":
            # 지정된 순번 (범위 제한)
            selected_index = image_index % total_count
            self.last_loaded_index = selected_index
        elif load_mode == "random":
            # seed 기반 결정적 랜덤 (매 실행마다 다른 값)
            # seed 가 같으면 같은 랜덤, seed 가 다르면 다른 랜덤
            random.seed(seed + self.seed_counter)
            selected_index = random.randint(0, total_count - 1)
            self.seed_counter += 1
            # sequential 모드와 충돌 방지 위해 리셋
            self.last_loaded_index = -1
        
        # 이미지 파일 로드
        selected_path = self.image_files[selected_index]
        try:
            img = Image.open(selected_path)
            image = self._pil_to_tensor(img.convert("RGB"))
            mask = self._image_to_mask(image)
            return (image, mask, selected_path, selected_index, total_count)
        except Exception as e:
            print(f"[LoadImage] Failed to load {selected_path}: {e}")
            # 실패 시 빈 이미지 반환
            empty_image = torch.zeros((1, 512, 512, 3), dtype=torch.float32)
            empty_mask = torch.ones((1, 512, 512), dtype=torch.float32)
            return (empty_image, empty_mask, selected_path, selected_index, total_count)
    
    def _scan_image_files(self, directory):
        """디렉토리에서 이미지 파일 스캔"""
        image_extensions = ['*.png', '*.jpg', '*.jpeg', '*.gif', '*.bmp', '*.webp', '*.tiff', '*.tif']
        files = []
        
        # SMB 경로 지원 (\\server\share 형태)
        search_dir = directory
        
        for ext in image_extensions:
            pattern = os.path.join(search_dir, ext)
            try:
                found = glob.glob(pattern)
                files.extend(found)
            except Exception as e:
                print(f"[LoadImage] Scan failed for {ext}: {e}")
        
        # 자연 정렬 (natural sort) - 숫자 포함 파일명 정렬
        files.sort(key=lambda x: self._natural_sort_key(x))
        return files
    
    def _natural_sort_key(self, s):
        """자연 정렬을 위한 키 함수 (예: img1, img2, img10)"""
        import re
        return [int(text) if text.isdigit() else text.lower()
                for text in re.split('([0-9]+)', s)]
    
    def _pil_to_tensor(self, pil_image):
        """PIL 이미지를 Tensor 로 변환"""
        image = np.array(pil_image).astype(np.float32) / 255.0
        image = torch.from_numpy(image)[None,]
        return image
    
    def _image_to_mask(self, image):
        """이미지에서 알파 채널 마스크 추출"""
        # 단색 이미지 처리를 위한 기본 마스크
        if image.shape[3] == 1:
            return torch.ones((image.shape[0], image.shape[1], image.shape[2]), dtype=torch.float32)
        
        # RGB 이미지를 마스크로 변환 (모든 픽셀을 1.0 으로)
        return torch.ones((image.shape[0], image.shape[1], image.shape[2]), dtype=torch.float32)
            

class ImageGridWithCaptions:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "layout_mode": (["custom", "horizontal", "vertical", "auto_square"], {"default": "custom"}),
                "columns": ("INT", {"default": 2, "min": 1, "max": 64, "step": 1}),
                "rows": ("INT", {"default": 2, "min": 1, "max": 64, "step": 1}),
                "grid_spec": ("STRING", {"default": "", "multiline": False}),
                "gap": ("INT", {"default": 0, "min": 0, "max": 1024, "step": 1}),
                "background_color": ("STRING", {"default": "#ffffff", "multiline": False}),
                "margin_position": (["none", "top", "bottom", "both"], {"default": "bottom"}),
                "margin_size": ("INT", {"default": 96, "min": 0, "max": 4096, "step": 1}),
                "captions": ("STRING", {"default": "", "multiline": True}),
                "caption_font_size": ("INT", {"default": 36, "min": 1, "max": 512, "step": 1}),
                "caption_align": (["center", "left", "right"], {"default": "center"}),
                "text_color": ("STRING", {"default": "#000000", "multiline": False}),
                "font_path": ("STRING", {"default": "", "multiline": False}),
                "use_caption_margin": ("BOOLEAN", {"default": False, "label_on": "Use Caption/Margin", "label_off": "No Caption/Margin"}),
                "show_generation_info": ("BOOLEAN", {"default": False, "label_on": "Show Prompt / LoRA", "label_off": "Hide Prompt / LoRA"}),
                "translation": (["Off", "Google", "Papago"], {"default": "Off"}),
            },
            "optional": {
                "text": ("STRING", {"forceInput": True}),
                "lora_info": ("JH_LORA_INFO",),
                "images": ("IMAGE",),
                "image_2": ("IMAGE",),
                "image_3": ("IMAGE",),
                "image_4": ("IMAGE",),
                "image_5": ("IMAGE",),
                "image_6": ("IMAGE",),
                "image_7": ("IMAGE",),
                "image_8": ("IMAGE",),
                "image_9": ("IMAGE",),
                "image_10": ("IMAGE",),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "make_grid"
    CATEGORY = JH_IMAGE_CATEGORY

    def _fit_grid_size(self, count, columns, rows):
        columns = max(1, int(columns))
        rows = max(1, int(rows))
        if columns * rows == count:
            return columns, rows

        target_ratio = columns / rows
        best = (float("inf"), float("inf"), 0, count, 1)
        for candidate_rows in range(1, int(math.sqrt(count)) + 1):
            if count % candidate_rows != 0:
                continue
            for candidate_columns, resolved_rows in ((count // candidate_rows, candidate_rows), (candidate_rows, count // candidate_rows)):
                ratio = candidate_columns / resolved_rows
                score = abs(math.log(ratio / target_ratio))
                distance = abs(candidate_columns - columns) + abs(resolved_rows - rows)
                best = min(best, (score, distance, -candidate_columns, candidate_columns, resolved_rows))
        return best[3], best[4]

    def _resolve_grid_size(self, count, layout_mode, columns, rows, grid_spec):
        spec = str(grid_spec or "").lower().replace(" ", "")
        if spec:
            parts = [part for part in spec.replace(",", "x").split("x") if part]
            try:
                if len(parts) == 2:
                    return self._fit_grid_size(count, int(parts[0]), int(parts[1]))
                if len(parts) > 2:
                    return len(parts), 1
            except ValueError:
                pass

        if layout_mode == "horizontal":
            return max(1, count), 1
        if layout_mode == "vertical":
            return 1, max(1, count)
        if layout_mode == "auto_square":
            resolved_columns = max(1, math.ceil(math.sqrt(count)))
            resolved_rows = max(1, math.ceil(count / resolved_columns))
            return self._fit_grid_size(count, resolved_columns, resolved_rows)

        return self._fit_grid_size(count, columns, rows)

    def _is_valid_image_batch(self, image_batch):
        if image_batch is None:
            return False
        try:
            if len(image_batch) == 0:
                return False
            if hasattr(image_batch, "numel") and image_batch.numel() == 0:
                return False
            if hasattr(image_batch, "shape") and len(image_batch.shape) < 4:
                return False
        except:
            return False
        return True

    def _collect_images(self, images, extra_images):
        collected = []
        for slot_index, image_batch in enumerate([images] + list(extra_images)):
            if not self._is_valid_image_batch(image_batch):
                continue
            for image in image_batch:
                try:
                    if hasattr(image, "numel") and image.numel() > 0:
                        collected.append((image, slot_index))
                except:
                    pass
        return collected

    def make_grid(self, layout_mode="custom", columns=2, rows=2, grid_spec="", gap=0, background_color="#ffffff", margin_position="bottom", margin_size=96, captions="", caption_font_size=36, caption_align="center", text_color="#000000", font_path="", use_caption_margin=False, show_generation_info=False, translation="Off", text="", lora_info=None, images=None, image_2=None, image_3=None, image_4=None, image_5=None, image_6=None, image_7=None, image_8=None, image_9=None, image_10=None):
        image_entries = self._collect_images(images, [
            image_2, image_3, image_4, image_5, image_6, image_7, image_8,
            image_9, image_10,
        ])
        count = len(image_entries)
        if count == 0:
            return (torch.zeros((1, 512, 512, 3), dtype=torch.float32),)

        tile_captions = _split_captions(captions, 10)
        tiles = []
        for image, caption_index in image_entries:
            tile = _image_tensor_to_pil(image)
            if use_caption_margin:
                tile = _add_margin_and_caption(
                    tile,
                    margin_position=margin_position,
                    margin_size=margin_size,
                    caption=tile_captions[caption_index] if caption_index < len(tile_captions) else "",
                    caption_font_size=caption_font_size,
                    caption_align=caption_align,
                    background_color=background_color,
                    text_color=text_color,
                    font_path=font_path,
                )
            tiles.append(tile)

        columns, rows = self._resolve_grid_size(count, layout_mode, columns, rows, grid_spec)

        cell_width = max(tile.width for tile in tiles)
        cell_height = max(tile.height for tile in tiles)
        gap = max(0, int(gap))
        background = _parse_rgb_color(background_color, (255, 255, 255))
        grid_width = columns * cell_width + gap * max(0, columns - 1)
        grid_height = rows * cell_height + gap * max(0, rows - 1)
        grid = Image.new("RGB", (grid_width, grid_height), background)

        for idx, tile in enumerate(tiles):
            col = idx % columns
            row = idx // columns
            if not use_caption_margin and tile.size != (cell_width, cell_height):
                tile = ImageOps.fit(tile, (cell_width, cell_height), method=Image.Resampling.LANCZOS)
            x = col * (cell_width + gap) + (cell_width - tile.width) // 2
            y = row * (cell_height + gap) + (cell_height - tile.height) // 2
            grid.paste(tile, (x, y))

        if show_generation_info:
            translated_text = translate_to_korean(translation, text)
            grid = _add_generation_info_panel(
                grid,
                text=text,
                translated_text=translated_text,
                lora_info=lora_info,
                font_size=caption_font_size,
                background_color=background_color,
                text_color=text_color,
                font_path=font_path,
            )

        return (_pil_to_image_tensor(grid),)


def _store_workflow_node_property(extra_pnginfo, unique_id, name, value):
    if not isinstance(extra_pnginfo, dict) or unique_id is None:
        return
    workflow = extra_pnginfo.get("workflow")
    if not isinstance(workflow, dict):
        return
    node_ids = {str(unique_id), str(unique_id).rsplit(":", 1)[-1]}
    node_lists = [workflow.get("nodes", [])]
    definitions = workflow.get("definitions")
    subgraphs = definitions.get("subgraphs", []) if isinstance(definitions, dict) else []
    node_lists.extend(subgraph.get("nodes", []) for subgraph in subgraphs if isinstance(subgraph, dict))
    for nodes in node_lists:
        if not isinstance(nodes, list):
            continue
        node = next((item for item in nodes if isinstance(item, dict) and str(item.get("id")) in node_ids), None)
        if node is not None:
            if not isinstance(node.get("properties"), dict):
                node["properties"] = {}
            node["properties"][name] = value
            return


def _get_workflow_node_property(extra_pnginfo, unique_id, name, default=None):
    if not isinstance(extra_pnginfo, dict) or unique_id is None:
        return default
    workflow = extra_pnginfo.get("workflow")
    if not isinstance(workflow, dict):
        return default
    node_ids = {str(unique_id), str(unique_id).rsplit(":", 1)[-1]}
    node_lists = [workflow.get("nodes", [])]
    definitions = workflow.get("definitions")
    subgraphs = definitions.get("subgraphs", []) if isinstance(definitions, dict) else []
    node_lists.extend(subgraph.get("nodes", []) for subgraph in subgraphs if isinstance(subgraph, dict))
    for nodes in node_lists:
        if not isinstance(nodes, list):
            continue
        node = next((item for item in nodes if isinstance(item, dict) and str(item.get("id")) in node_ids), None)
        properties = node.get("properties") if isinstance(node, dict) else None
        if isinstance(properties, dict):
            return properties.get(name, default)
    return default


class JHClipboardText:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "text": ("STRING", {"default": "", "multiline": True}),
            },
            "optional": {
                "clipboard_override": ("STRING", {"default": ""}),
                "display_text": ("STRING", {"default": "", "multiline": True}),
                "translation_provider": (["Papago", "Google"], {"default": "Papago"}),
                "translate_en": ("BOOLEAN", {"default": False}),
                "translate_kr": ("BOOLEAN", {"default": False}),
                "display_translated_text": ("STRING", {"default": "", "multiline": True}),
                "manual_text": ("STRING", {"default": "", "multiline": True}),
                "use_manual_text": ("BOOLEAN", {"default": False}),
            },
            "hidden": {
                "unique_id": "UNIQUE_ID",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("text", "translated_text")
    FUNCTION = "get_text"
    CATEGORY = JH_UTILS_CATEGORY

    def get_text(self, text="", clipboard_override="", display_text="", translation_provider="Papago", translate_en=False, translate_kr=False, display_translated_text="", manual_text="", use_manual_text=False, unique_id=None, extra_pnginfo=None):
        original_text = manual_text if use_manual_text else text
        if clipboard_override:
            try:
                override = json.loads(clipboard_override)
            except (json.JSONDecodeError, TypeError):
                override = None
            if isinstance(override, dict) and override.get("force") is True and isinstance(override.get("text"), str):
                original_text = override["text"]
        translated_text = ""
        target_language = "ko" if translate_kr else ("en" if translate_en else "")
        translation_cache_key = ""
        if target_language:
            cache_source = f"{translation_provider}\0{target_language}\0{original_text}".encode("utf-8")
            translation_cache_key = hashlib.sha256(cache_source).hexdigest()
            previous_cache_key = _get_workflow_node_property(
                extra_pnginfo, unique_id, "jh_clipboard_translation_cache_key", ""
            )
            if previous_cache_key == translation_cache_key and display_translated_text:
                translated_text = display_translated_text
            elif target_language == "ko":
                translated_text = translate_to_korean(translation_provider, original_text)
            else:
                translated_text = translate_to_english(translation_provider, original_text)
        _store_workflow_node_property(extra_pnginfo, unique_id, "jh_clipboard_output", original_text)
        _store_workflow_node_property(extra_pnginfo, unique_id, "jh_clipboard_translated_output", translated_text)
        _store_workflow_node_property(extra_pnginfo, unique_id, "jh_clipboard_translation_cache_key", translation_cache_key)
        return {
            "ui": {
                "text": [original_text],
                "original_text": [original_text],
                "translated_text": [translated_text],
                "translation_cache_key": [translation_cache_key],
            },
            "result": (original_text, translated_text),
        }


class JHPromptBuilder(comfy_io.ComfyNode):
    @classmethod
    def define_schema(cls):
        input_template = comfy_io.Autogrow.TemplateNames(
            input=comfy_io.String.Input("input_prompt", optional=True),
            names=[f"input_prompt{i}" for i in range(1, 101)],
            min=1,
        )
        return comfy_io.Schema(
            node_id="JHPromptBuilder",
            display_name="JH Prompt Builder",
            category=JH_UTILS_CATEGORY,
            inputs=[
                comfy_io.String.Input("base_prompt", default="", multiline=True),
                comfy_io.String.Input("prompt_slots", default="[]", socketless=True),
                comfy_io.Int.Input("base_position", default=0, min=0, socketless=True),
                comfy_io.String.Input("prompt_order", default="[]", socketless=True),
                comfy_io.Float.Input("base_strength", default=1.0, socketless=True),
                comfy_io.String.Input("input_prompt_strengths", default="{}", socketless=True),
                comfy_io.Boolean.Input("base_translate", default=False, socketless=True),
                comfy_io.String.Input("input_prompt_translations", default="{}", socketless=True),
                comfy_io.Combo.Input("translation_provider", display_name="Translation", options=["Papago", "Google"], default="Papago"),
                comfy_io.Boolean.Input("base_enabled", default=True, socketless=True),
                comfy_io.String.Input("input_prompt_enabled", default="{}", socketless=True),
                comfy_io.Boolean.Input("base_translate_kr", default=False, socketless=True),
                comfy_io.String.Input("input_prompt_translations_kr", default="{}", socketless=True),
                comfy_io.Autogrow.Input("input_prompts", template=input_template, optional=True),
            ],
            outputs=[comfy_io.String.Output("prompt")],
        )

    @classmethod
    def execute(cls, base_prompt="", prompt_slots="[]", base_position=0, prompt_order="[]", base_strength=1.0, input_prompt_strengths="{}", base_translate=False, input_prompt_translations="{}", base_translate_kr=False, input_prompt_translations_kr="{}", translation_provider="Papago", base_enabled=True, input_prompt_enabled="{}", input_prompts=None):
        try:
            slots = json.loads(prompt_slots) if isinstance(prompt_slots, str) else prompt_slots
        except json.JSONDecodeError as error:
            raise ValueError("Prompt slots have an invalid format") from error
        if not isinstance(slots, list):
            raise ValueError("Prompt slots have an invalid format")

        slot_prompts = {}
        slot_order = []
        for index, slot in enumerate(slots):
            if not isinstance(slot, dict) or not isinstance(slot.get("prompt", ""), str):
                raise ValueError("Prompt slots have an invalid format")
            prompt = slot.get("prompt", "")
            if not slot.get("enabled", True) or not prompt.strip():
                prompt = ""
            else:
                if slot.get("translate_kr", False):
                    prompt = translate_to_korean(translation_provider, prompt)
                elif slot.get("translate", False):
                    prompt = translate_to_english(translation_provider, prompt)
                strength = float(slot.get("strength", 1.0))
                prompt = prompt if strength == 1.0 else f"({prompt}:{strength:.6g})"
            token = f"slot:{slot.get('id', index)}"
            slot_prompts[token] = prompt
            slot_order.append(token)

        try:
            input_strengths = json.loads(input_prompt_strengths) if isinstance(input_prompt_strengths, str) else input_prompt_strengths
        except json.JSONDecodeError as error:
            raise ValueError("Input prompt strengths have an invalid format") from error
        if not isinstance(input_strengths, dict):
            raise ValueError("Input prompt strengths have an invalid format")
        try:
            input_translations = json.loads(input_prompt_translations) if isinstance(input_prompt_translations, str) else input_prompt_translations
        except json.JSONDecodeError as error:
            raise ValueError("Input prompt translations have an invalid format") from error
        if not isinstance(input_translations, dict):
            raise ValueError("Input prompt translations have an invalid format")
        try:
            input_translations_kr = json.loads(input_prompt_translations_kr) if isinstance(input_prompt_translations_kr, str) else input_prompt_translations_kr
        except json.JSONDecodeError as error:
            raise ValueError("Input prompt Korean translations have an invalid format") from error
        if not isinstance(input_translations_kr, dict):
            raise ValueError("Input prompt Korean translations have an invalid format")
        try:
            input_enabled = json.loads(input_prompt_enabled) if isinstance(input_prompt_enabled, str) else input_prompt_enabled
        except json.JSONDecodeError as error:
            raise ValueError("Input prompt enabled states have an invalid format") from error
        if not isinstance(input_enabled, dict):
            raise ValueError("Input prompt enabled states have an invalid format")

        def apply_strength(prompt, strength):
            strength = float(strength)
            return prompt if strength == 1.0 else f"({prompt}:{strength:.6g})"

        def translate_prompt(prompt, translate_en, translate_kr):
            if translate_kr:
                return translate_to_korean(translation_provider, prompt)
            return translate_to_english(translation_provider, prompt) if translate_en else prompt

        input_prompts = input_prompts or {}
        input_order = sorted(input_prompts, key=lambda name: int(name.rsplit("input_prompt", 1)[-1]))
        values = {"base": apply_strength(translate_prompt(base_prompt, base_translate, base_translate_kr), base_strength) if base_enabled and base_prompt.strip() else ""}
        values.update(slot_prompts)
        values.update({f"input:{name}": apply_strength(translate_prompt(value, input_translations.get(name, False), input_translations_kr.get(name, False)), input_strengths.get(name, 1.0)) for name, value in input_prompts.items() if input_enabled.get(name, True) and isinstance(value, str) and value.strip()})

        try:
            order = json.loads(prompt_order) if isinstance(prompt_order, str) else prompt_order
        except json.JSONDecodeError as error:
            raise ValueError("Prompt order has an invalid format") from error
        if not isinstance(order, list) or not all(isinstance(token, str) for token in order):
            raise ValueError("Prompt order has an invalid format")
        if not order:
            order = list(slot_order)
            order.insert(max(0, min(int(base_position), len(order))), "base")

        available_order = ["base", *slot_order, *(f"input:{name}" for name in input_order)]
        order = [token for token in order if token in values]
        order.extend(token for token in available_order if token not in order)
        prompts = [values[token] for token in order if values.get(token, "").strip()]
        return comfy_io.NodeOutput(", ".join(prompts))


class JHClipboardImage:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "image_data": ("STRING", {"default": "", "multiline": True}),
                "previous_image_data": ("STRING", {"default": "", "multiline": True}),
            }
        }

    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("image", "mask")
    FUNCTION = "load_image"
    CATEGORY = JH_IMAGE_CATEGORY

    def load_image(self, image_data="", previous_image_data=""):
        img = _clipboard_image_value_to_pil(image_data)
        if img is None:
            image, mask = _empty_image_and_mask()
            return (image, mask)

        image = _pil_to_image_tensor(img)
        mask = torch.ones((1, img.height, img.width), dtype=torch.float32)
        return (image, mask)


class JHLoraLoader(LoraLoader):
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL",),
                "clip": ("CLIP",),
                "additional_loras": ("STRING", {"default": "[]", "multiline": True}),
            },
            "optional": {
                "previous_lora_info": ("JH_LORA_INFO",),
            },
        }

    RETURN_TYPES = ("MODEL", "CLIP", "JH_LORA_INFO")
    RETURN_NAMES = ("model", "clip", "lora_info")
    FUNCTION = "load_lora_with_info"
    CATEGORY = JH_UTILS_CATEGORY

    def load_lora_with_info(self, model, clip, additional_loras="[]", previous_lora_info=None):
        lora_info = list(previous_lora_info or [])
        for lora in _parse_lora_slots(additional_loras):
            if not lora["enabled"] or not lora["name"] or lora["name"] == "None" or (lora["strength_model"] == 0 and lora["strength_clip"] == 0):
                continue
            model, clip = self.load_lora(model, clip, lora["name"], lora["strength_model"], lora["strength_clip"])
            lora_info.append({"name": lora["name"], "strength_model": lora["strength_model"], "strength_clip": lora["strength_clip"]})
        return (model, clip, lora_info)


class JHShowText:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "text": ("STRING", {"forceInput": True}),
                "display_text": ("STRING", {"default": "", "multiline": True}),
                "display_lora_info": ("STRING", {"default": "", "multiline": True}),
            },
            "optional": {
                "lora_info": ("JH_LORA_INFO",),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    FUNCTION = "show_text"
    OUTPUT_NODE = True
    CATEGORY = JH_UTILS_CATEGORY

    def show_text(self, text="", display_text="", display_lora_info="", lora_info=None):
        loras = _normalize_loras(lora_info or [])
        return {"ui": {"text": [text], "lora_info": [json.dumps(loras, ensure_ascii=False)]}, "result": (text,)}


class _JHSavedTextSelection:
    def __init__(self):
        self.seen_items = set()
        self.draw_count = 0

    def select_item(self, cycle_without_repeats):
        with JH_SAVED_TEXTS_LOCK:
            saved_items = _load_saved_texts()
        if not saved_items:
            raise ValueError("No saved texts. Save one from JH Show Text first.")

        identified_items = [(json.dumps(item, ensure_ascii=False, sort_keys=True), item) for item in saved_items]
        if not cycle_without_repeats:
            self.seen_items.clear()
            item = random.choice(saved_items)
            position = None
        else:
            current_items = {identifier for identifier, _ in identified_items}
            self.seen_items.intersection_update(current_items)
            choices = [(identifier, item) for identifier, item in identified_items if identifier not in self.seen_items]
            if not choices:
                self.seen_items.clear()
                choices = identified_items
            identifier, item = random.choice(choices)
            self.seen_items.add(identifier)
            position = len(self.seen_items)

        self.draw_count += 1
        if position is None:
            status = f"Random draw #{self.draw_count} · {len(saved_items)} saved"
        else:
            status = f"Cycle {position}/{len(saved_items)} · draw #{self.draw_count}"
        status += f" · {len(item['loras'])} LoRA" if item["loras"] else " · no LoRA"
        return item, status


class JHSavedPicker(_JHSavedTextSelection):
    def __init__(self):
        super().__init__()
        self.lora_loader = LoraLoader()

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL",),
                "clip": ("CLIP",),
                "cycle_without_repeats": ("BOOLEAN", {"default": False, "label_on": "cycle", "label_off": "random"}),
                "additional_loras": ("STRING", {"default": "[]", "multiline": True}),
            }
        }

    RETURN_TYPES = ("STRING", "MODEL", "CLIP")
    RETURN_NAMES = ("text", "model", "clip")
    FUNCTION = "pick_text_and_lora"
    CATEGORY = JH_UTILS_CATEGORY

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return random.random()

    def pick_text_and_lora(self, model, clip, cycle_without_repeats=False, additional_loras="[]"):
        item, status = self.select_item(cycle_without_repeats)
        for lora in item["loras"]:
            model, clip = self.lora_loader.load_lora(model, clip, lora["name"], lora["strength_model"], lora["strength_clip"])
        manual_loras = []
        for lora in _parse_lora_slots(additional_loras):
            if not lora["enabled"] or not lora["name"] or lora["name"] == "None" or (lora["strength_model"] == 0 and lora["strength_clip"] == 0):
                continue
            model, clip = self.lora_loader.load_lora(model, clip, lora["name"], lora["strength_model"], lora["strength_clip"])
            manual_loras.append({"name": lora["name"], "strength_model": lora["strength_model"], "strength_clip": lora["strength_clip"]})
        if manual_loras:
            status += f" + {len(manual_loras)} manual LoRA"
        applied_loras = item["loras"] + manual_loras
        return {"ui": {"text": [item["text"]], "status": [status], "lora_info": [json.dumps(applied_loras, ensure_ascii=False)]}, "result": (item["text"], model, clip)}


class JHSavedTextPicker(JHSavedPicker):
    DEPRECATED = True


class JHSavedTextLoraPicker(JHSavedPicker):
    DEPRECATED = True


class JHLlamaPrompt:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "prompt": ("STRING", {"default": "", "multiline": True}),
                "mode": (["generate", "enhance"],),
                "instruction": ("STRING", {"default": "", "multiline": True}),
                "enable_vision": ("BOOLEAN", {"default": False}),
                "vision_max_size": ("INT", {"default": 1024, "min": 256, "max": 4096, "step": 64}),
                "video_frames": ("INT", {"default": 4, "min": 1, "max": 16}),
                "server_url": ("STRING", {"default": "http://127.0.0.1:8082"}),
                "model": ("STRING", {"default": "auto"}),
                "max_tokens": ("INT", {"default": 768, "min": 1, "max": 8192}),
                "temperature": ("FLOAT", {"default": 0.7, "min": 0.0, "max": 2.0, "step": 0.05}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff, "control_after_generate": True}),
                "timeout": ("INT", {"default": 300, "min": 1, "max": 3600}),
                "display_text": ("STRING", {"default": "", "multiline": True}),
                "display_lora_info": ("STRING", {"default": "", "multiline": True}),
                "character_sheet_mode": (["Normal", "Character sheet", "Combined"], {"default": "Normal"}),
                "curvy_mode": ("BOOLEAN", {"default": False, "label_on": "On", "label_off": "Off"}),
                "glamorous_mode": ("BOOLEAN", {"default": False, "label_on": "On", "label_off": "Off"}),
                "huge_breasts_mode": ("BOOLEAN", {"default": False, "label_on": "On", "label_off": "Off"}),
                "character_identity": (["LoRA / model", "Reference image"],),
                "identity_trigger": ("STRING", {"default": ""}),
                "curvy_strength": ("FLOAT", {"default": 1.0, "min": 0.25, "max": 3.0, "step": 0.05}),
                "glamorous_strength": ("FLOAT", {"default": 1.0, "min": 0.25, "max": 3.0, "step": 0.05}),
                "huge_breasts_strength": ("FLOAT", {"default": 1.0, "min": 0.25, "max": 3.0, "step": 0.05}),
                "reuse_identical_image": ("BOOLEAN", {"default": False, "label_on": "Reuse", "label_off": "Refresh"}),
                "supplemental_prompt": ("STRING", {"default": "", "multiline": True}),
                "supplemental_position": (["After base prompt", "Before base prompt"], {"default": "After base prompt"}),
                "translation_provider": (["Papago", "Google"], {"default": "Papago"}),
                "translate_prompt": ("BOOLEAN", {"default": False}),
                "translate_instruction": ("BOOLEAN", {"default": False}),
                "translate_supplemental": ("BOOLEAN", {"default": False}),
            },
            "optional": {
                "image": ("IMAGE", {"lazy": True}),
                "video": ("VIDEO", {"lazy": True}),
                "lora_info": ("JH_LORA_INFO",),
            },
            "hidden": {"unique_id": "UNIQUE_ID", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("prompt",)
    OUTPUT_IS_LIST = (True,)
    FUNCTION = "create_prompt"
    OUTPUT_NODE = True
    CATEGORY = JH_UTILS_CATEGORY

    @classmethod
    def IS_CHANGED(cls, reuse_identical_image=False, **kwargs):
        return False if reuse_identical_image else float("nan")

    @classmethod
    def check_lazy_status(cls, enable_vision, **kwargs):
        if not enable_vision:
            return []
        return [name for name in ("image", "video") if name in kwargs and kwargs[name] is None]

    def _request_json(self, url, payload=None, timeout=300):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"llama.cpp request failed ({error.code}): {detail}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Could not connect to llama.cpp at {url}: {error.reason}") from error

    def _resolve_model(self, server_url, model, timeout):
        if model.strip().lower() != "auto":
            return model.strip()
        response = self._request_json(f"{server_url}/v1/models", timeout=timeout)
        models = response.get("data") or response.get("models") or []
        if not models:
            raise RuntimeError("llama.cpp returned no loaded models")
        return models[0].get("id") or models[0].get("model") or models[0].get("name")

    def _wait_for_vram_handoff(self, server_url, timeout):
        """Wait until the managed llama backend has finished its idle unload.

        The completion response can arrive before llama.cpp's idle-sleep thread
        releases CUDA allocations.  Starting the next ComfyUI model load in
        that window fills the card even though PyTorch itself owns almost no
        memory.
        """
        parsed = urllib.parse.urlparse(server_url)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.port != 8082:
            return
        guard_host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        guard_url = parsed._replace(
            netloc=f"{guard_host}:8080",
            path="/__vram_guard/wait-backend-release",
            query=urllib.parse.urlencode({"timeout": min(120, max(10, timeout))}),
            fragment="",
        ).geturl()
        response = self._request_json(guard_url, timeout=min(125, max(15, timeout + 5)))
        if response.get("released") is not True:
            raise RuntimeError(f"llama.cpp VRAM handoff failed: {response}")

    def _pil_image_content(self, pil_image, max_size):
        pil_image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        pil_image.save(buffer, format="JPEG", quality=90)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}

    def _image_content(self, image, max_size):
        return self._pil_image_content(_image_tensor_to_pil(image), max_size)

    def _send_image_preview(self, image, unique_id):
        if image is None or unique_id is None:
            return
        preview = _image_tensor_to_pil(image[0])
        preview.thumbnail((640, 640), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        preview.save(buffer, format="JPEG", quality=88)
        data_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
        PromptServer.instance.send_sync(
            "jh-llama-image-preview",
            {"node_id": str(unique_id), "image": data_url},
            PromptServer.instance.client_id,
        )

    def _video_contents(self, video, frame_count, max_size):
        start_time, _ = video.get_active_trim_window()
        total_frames = video.get_frame_count()
        if total_frames < 1:
            raise ValueError("The connected video has no frames")
        sample_count = min(frame_count, total_frames)
        targets = [total_frames // 2] if sample_count == 1 else [round(index * (total_frames - 1) / (sample_count - 1)) for index in range(sample_count)]
        source = video.get_stream_source()
        if isinstance(source, io.BytesIO):
            source.seek(0)
        contents = []
        with av.open(source, mode="r") as container:
            stream = container.streams.video[0]
            if start_time > 0:
                container.seek(int(start_time / av.time_base), backward=True)
            target_index = 0
            frame_index = -1
            for frame in container.decode(stream):
                if frame.time is not None and float(frame.time) < start_time:
                    continue
                frame_index += 1
                if frame_index < targets[target_index]:
                    continue
                contents.append({"type": "text", "text": f"Video frame {target_index + 1} of {len(targets)}."})
                contents.append(self._pil_image_content(frame.to_image().convert("RGB"), max_size))
                target_index += 1
                if target_index == len(targets):
                    break
        if not contents:
            raise ValueError("No frames could be decoded from the video")
        return contents

    def _system_prompt(self, mode, instruction, enable_vision, character_sheet_mode, character_identity, curvy_mode, glamorous_mode, huge_breasts_mode, curvy_strength, glamorous_strength, huge_breasts_strength):
        if character_sheet_mode:
            system_prompt = (
                "Write only a compact, generation-ready description of the one clearly adult character visible in the reference. Do not describe a character-sheet layout; the application adds it separately. "
                "Begin with the character's most distinctive observed appearance, then describe complexion, exact hair color and haircut, face and makeup, body proportions, clothing and footwear, and finally accessories, tattoos, piercings, scars, and other identifying details. "
                "Treat visible traits as authoritative. Do not replace the person with a generic, idealized, beautified, or stereotyped character, and do not recolor hair or skin, change the haircut, remove clothing details, body art, or piercings, or invent ethnicity. "
                "Preserve permanent or wearable identity details, but omit the source pose, hand gesture, transient facial expression, action, setting, camera angle, framing, and composition. "
                "Describe a neutral, relaxed identity-reference presentation. Reinterpret non-photographic elements as physically plausible real-world clothing, accessories, makeup, prosthetics, and materials. "
                "Return one cohesive English paragraph of 120–200 words with no layout instructions, preface, reasoning, commentary, or <think>."
            )
        elif mode == "enhance":
            system_prompt = "Improve the user's image or video generation prompt. Preserve its intent and important details. Return only the improved prompt without commentary."
        else:
            system_prompt = "Create a detailed image or video generation prompt from the user's request. Return only the final prompt without commentary."
        if enable_vision:
            if character_sheet_mode:
                if character_identity == "Reference image":
                    system_prompt += " Preserve the primary character's visible facial identity."
                else:
                    system_prompt += " Leave facial identity to the downstream model or identity LoRA; do not describe ethnicity or identity-defining facial geometry."
            else:
                system_prompt += " Analyze the attached visual media and use its visible subjects, setting, composition, and style. For chronological video frames, compare them to infer motion without inventing unsupported changes."
        if instruction.strip() and not character_sheet_mode:
            system_prompt += f"\nAdditional requirements: {instruction.strip()}"
        if character_sheet_mode:
            system_prompt += "\nIgnore the normal instruction field while character-sheet mode is active."

        body_requirements = []
        if curvy_mode:
            strength = max(0.25, min(3.0, float(curvy_strength)))
            degree = "clearly pronounced" if strength < 1.5 else "strongly exaggerated" if strength < 2.5 else "extreme and visually dominant"
            body_requirements.append(f"curvy intensity {strength:.2f}/3.00: give each clearly adult feminine primary character a curvy silhouette with {degree} emphasis, a sharply defined waist, substantially fuller hips, rounded buttocks, and full thighs while retaining coherent physically plausible anatomy")
        if glamorous_mode:
            strength = max(0.25, min(3.0, float(glamorous_strength)))
            degree = "unmistakably voluptuous" if strength < 1.5 else "dramatically voluptuous" if strength < 2.5 else "extremely voluptuous and visually dominant"
            body_requirements.append(f"glamorous intensity {strength:.2f}/3.00: give each clearly adult feminine primary character an hourglass physique that is {degree}, with elegant confident posture, polished makeup where appropriate, and a refined photographic presence without changing the observed hairstyle")
        if huge_breasts_mode:
            strength = max(0.25, min(3.0, float(huge_breasts_strength)))
            if strength < 1.5:
                requirement = "an exceptionally large, full, heavy, strongly projecting bust that is clearly larger than the unmodified reference"
            elif strength < 2.5:
                requirement = "a dramatically oversized, heavy bust with very strong forward projection and volume that dominates the upper torso and substantially exceeds the unmodified reference"
            else:
                requirement = "a gigantic, extraordinarily massive bust far beyond ordinary anatomical proportions, with each breast visibly comparable to or larger than the character's head in volume, extreme forward projection, and a combined silhouette substantially wider than the ribcage"
            body_requirements.append(f"bust intensity {strength:.2f}/3.00: give each clearly adult feminine primary character {requirement}; preserve photorealistic attachment, gravity, skin, and fabric behavior, but never use anatomical plausibility, clothing support, framing, or modesty to shrink, conceal, compress, or normalize the requested scale")
        if body_requirements and not character_sheet_mode:
            system_prompt += "\nMandatory adult-character body requirements: " + "; ".join(body_requirements) + ". Treat each numerical intensity as a direct visual-strength control: higher values require progressively more conspicuous proportions, not merely stronger adjectives. If the visual reference already shows the requested trait strongly, use its visible degree as the minimum rather than averaging it toward typical proportions. Apply these requirements consistently in every applicable view and do not weaken, omit, conceal, normalize, or euphemize them. Apply them only to characters who are clearly adults; never sexualize or age up a minor or age-ambiguous subject."
        return system_prompt

    @staticmethod
    def _character_sheet_user_prompt(prompt, enable_vision, character_identity):
        if enable_vision:
            identity_requirement = (
                "Preserve the primary adult character's visible facial identity. "
                if character_identity == "Reference image"
                else "Do not describe or copy identity-defining facial geometry or ethnicity; the downstream model or identity LoRA supplies the face. "
            )
            return (
                "Describe only the primary adult character in the attached visual media. Ignore the original text prompt entirely. Treat the attached character, not a familiar character-sheet archetype, as the sole source of appearance. "
                + identity_requirement
                + "Extract apparent age, visible complexion, exact hair, facial details permitted by the identity setting, body proportions, complete clothing design, footwear, accessories, tattoos, piercings, makeup, scars, and other distinctive visible traits. Do not invent ethnicity, recolor or restyle the character, remove visible details, or substitute a generic attractive person. Omit pose, gesture, transient expression, action, environment, and camera composition. Before responding, silently verify that every prominent visible appearance trait remains. Return only the compact character description; do not write or repeat layout instructions."
            )
        return (
            "Extract only the character's visible identity, appearance, clothing, footwear, accessories, and distinctive permanent traits from the source request below. "
            "Ignore any requested pose, expression, gesture, action, environment, camera framing, composition, or output format. Return only a compact character description and do not write layout instructions.\n\nSource request:\n" + prompt
        )

    @staticmethod
    def _merge_supplemental_prompt(base_prompt, supplemental_prompt, supplemental_position, character_sheet_mode):
        supplemental_prompt = str(supplemental_prompt or "").strip()
        if not supplemental_prompt:
            return base_prompt
        if character_sheet_mode:
            supplemental_prompt = (
                "Mandatory user-specified character override: " + supplemental_prompt
                + " This override takes priority over any conflicting source description or visual inference, especially for traits or clothing that are not visible in the reference. Integrate it into the final prompt without mentioning a conflict or an override."
            )
        if supplemental_position == "Before base prompt":
            return supplemental_prompt + "\n\n" + base_prompt
        return base_prompt + "\n\n" + supplemental_prompt

    @classmethod
    def _compose_character_sheet_prompt(cls, text):
        return (
            "A professional photographic identity reference board presents one clearly adult character as the same real person in exactly five isolated camera-captured studio photographs, never as a group portrait. "
            "In a portrait-oriented two-tier contact sheet, reserve the upper forty percent for a large direct-front head-and-shoulders close portrait and a large three-quarter face close portrait, with the face filling most of each frame for maximum identity detail. "
            "Use the lower sixty percent for three neck-down body-reference photographs showing front view, strict side-profile view, and back view at equal scale in relaxed natural standing poses, each framed from the base of the neck through the feet with the entire head and face outside the frame. "
            "Use the upper portraits as the only facial-identity references, keep apparent age, skin tone, observed hairstyle and base hair color, body proportions, clothing, footwear, and accessories consistent wherever visible, and preserve natural skin pores, individual hairs, subtle asymmetry, realistic fabric, and photographic lens depth. "
            "Separate every photograph with generous clean white gutters and no touching, overlap, occlusion, or interaction, using a plain warm-white studio backdrop and soft flattering photographic light. "
            "Use the following description only for the character's identity and appearance; discard any action, pose, environment, props, framing, or single-scene composition it mentions: "
            + text
        )

    @staticmethod
    def _enforce_body_modes(text, curvy_mode, glamorous_mode, huge_breasts_mode, curvy_strength, glamorous_strength, huge_breasts_strength):
        directives = []
        if curvy_mode:
            strength = max(0.25, min(3.0, float(curvy_strength)))
            degree = "noticeably stronger than the unmodified reference" if strength < 1.5 else "dramatically stronger than the unmodified reference" if strength < 2.5 else "extreme and dominant beyond the unmodified reference"
            directives.append(f"curvy intensity {strength:.2f}/3.00 requires a sharply defined waist with substantially fuller hips, rounded buttocks, and full thighs, {degree}")
        if glamorous_mode:
            strength = max(0.25, min(3.0, float(glamorous_strength)))
            degree = "unmistakably voluptuous" if strength < 1.5 else "dramatically voluptuous" if strength < 2.5 else "extremely voluptuous and visually dominant"
            directives.append(f"glamorous intensity {strength:.2f}/3.00 requires an hourglass silhouette that is {degree}, with confident refined presentation without changing the observed hairstyle")
        if huge_breasts_mode:
            strength = max(0.25, min(3.0, float(huge_breasts_strength)))
            if strength < 1.5:
                degree = "an exceptionally large, full, heavy, strongly projecting bust that is clearly larger than the unmodified reference and never merely average or moderately large"
            elif strength < 2.5:
                degree = "a dramatically oversized, heavy, strongly projecting bust that substantially exceeds the unmodified reference and dominates the upper-body silhouette"
            else:
                degree = "a gigantic, extraordinarily massive bust far beyond ordinary anatomical proportions, with each breast visibly comparable to or larger than the character's head in volume, extreme forward projection, and a combined silhouette substantially wider than the ribcage"
            directives.append(f"bust intensity {strength:.2f}/3.00 requires {degree}; clothing must stretch and contour around the full volume without flattening, supporting it into a smaller silhouette, concealing it, or cropping it, and realism applies to gravity, attachment, skin, and fabric behavior rather than limiting size")
        if not directives:
            return text
        return text.rstrip() + " Mandatory final body-proportion directives: " + "; ".join(directives) + ". Apply these directives only to clearly adult characters and do not omit, soften, normalize, or euphemize them."

    @staticmethod
    def _is_character_sheet_prompt(text):
        normalized = text.casefold()
        has_sheet = any(term in normalized for term in ("character sheet", "reference sheet", "turnaround sheet", "identity reference board", "contact sheet"))
        view_groups = (
            ("front view", "front-facing", "view from the front", "full-body front"),
            ("three-quarter", "three quarter", "3/4 view"),
            ("side-profile", "side profile", "side view", "profile view"),
            ("back view", "back-facing", "rear view", "view from behind", "and back views"),
        )
        return has_sheet and sum(any(term in normalized for term in group) for group in view_groups) >= 3

    @staticmethod
    def _repeats_character_sheet_layout(text):
        normalized = text.casefold()
        terms = ("contact-sheet", "contact sheet", "upper forty percent", "lower sixty percent", "exactly five", "neck-down", "neck down", "white gutters")
        return sum(term in normalized for term in terms) >= 2

    @staticmethod
    def _image_prompt_cache_key(image, model, system_prompt, user_prompt, vision_max_size, max_tokens, temperature, identity_trigger):
        digest = hashlib.sha256()
        image_count = min(int(image.shape[0]), 4)
        for index in range(image_count):
            pil_image = _image_tensor_to_pil(image[index])
            digest.update(f"{pil_image.width}x{pil_image.height}:{pil_image.mode}\n".encode("ascii"))
            digest.update(pil_image.tobytes())
        settings = json.dumps({
            "model": model,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "vision_max_size": vision_max_size,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "identity_trigger": identity_trigger,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest.update(settings.encode("utf-8"))
        return digest.hexdigest()

    @staticmethod
    def _send_cache_hit(unique_id):
        if unique_id is None:
            return
        PromptServer.instance.send_sync(
            "jh-llama-cache-hit",
            {"node_id": str(unique_id), "message": "Reused the cached prompt for this identical image and settings."},
            PromptServer.instance.client_id,
        )

    def create_prompt(self, prompt, mode, instruction, enable_vision, vision_max_size, video_frames, server_url, model, max_tokens, temperature, seed, timeout, display_text="", display_lora_info="", character_sheet_mode="Normal", curvy_mode=False, glamorous_mode=False, huge_breasts_mode=False, character_identity="LoRA / model", identity_trigger="", curvy_strength=1.0, glamorous_strength=1.0, huge_breasts_strength=1.0, reuse_identical_image=False, supplemental_prompt="", supplemental_position="After base prompt", translation_provider="Papago", translate_prompt=False, translate_instruction=False, translate_supplemental=False, image=None, video=None, lora_info=None, unique_id=None, extra_pnginfo=None):
        server_url = server_url.rstrip("/")
        parsed_url = urllib.parse.urlparse(server_url)
        # 8080 is the human-facing VRAM guard. A ComfyUI node calling it would
        # wait for its own running queue forever, so migrate old workflows to
        # the direct llama backend automatically.
        if parsed_url.hostname in {"127.0.0.1", "localhost", "::1"} and parsed_url.port == 8080:
            direct_host = f"[{parsed_url.hostname}]" if ":" in parsed_url.hostname else parsed_url.hostname
            server_url = parsed_url._replace(netloc=f"{direct_host}:8082").geturl()
            parsed_url = urllib.parse.urlparse(server_url)
        if parsed_url.scheme != "http" or parsed_url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("server_url must point to a local llama.cpp HTTP server")
        if enable_vision and image is None and video is None:
            raise ValueError("enable_vision is on, but no image or video is connected")
        if isinstance(character_sheet_mode, bool):
            output_mode = "Character sheet" if character_sheet_mode else "Normal"
        else:
            output_mode = str(character_sheet_mode or "Normal")
        if output_mode not in {"Normal", "Character sheet", "Combined"}:
            raise ValueError(f"Unknown prompt output mode: {output_mode}")
        if output_mode == "Combined" and image is not None and not getattr(image, "_jh_character_sheet_reference", True):
            output_mode = "Normal"

        translated_prompt = translate_to_english(translation_provider, prompt) if translate_prompt else prompt
        translated_instruction = translate_to_english(translation_provider, instruction) if translate_instruction else instruction
        if translate_supplemental:
            supplemental_prompt = translate_to_english(translation_provider, supplemental_prompt)
        self._send_image_preview(image, unique_id)
        model = self._resolve_model(server_url, model, timeout)
        identity_trigger = str(identity_trigger or "").strip()
        if identity_trigger.casefold() in {"none", "null", "undefined"}:
            identity_trigger = ""

        media_content = []
        if enable_vision:
            if image is not None:
                image_count = min(int(image.shape[0]), 4)
                media_content.extend(self._image_content(image[index], vision_max_size) for index in range(image_count))
            if video is not None:
                media_content.append({"type": "text", "text": "The following images are chronological frames sampled from one video."})
                media_content.extend(self._video_contents(video, video_frames, vision_max_size))

        sheet_modes = [False, True] if output_mode == "Combined" else [output_mode == "Character sheet"]
        results = {}
        models_unloaded = False
        cache_hit = False
        for sheet_mode in sheet_modes:
            mode_prompt = prompt if sheet_mode and enable_vision else translated_prompt
            mode_instruction = instruction if sheet_mode else translated_instruction
            system_prompt = self._system_prompt(mode, mode_instruction, enable_vision, sheet_mode, character_identity, curvy_mode, glamorous_mode, huge_breasts_mode, curvy_strength, glamorous_strength, huge_breasts_strength)
            user_prompt = self._character_sheet_user_prompt(mode_prompt, enable_vision, character_identity) if sheet_mode else mode_prompt
            user_prompt = self._merge_supplemental_prompt(user_prompt, supplemental_prompt, supplemental_position, sheet_mode)
            cache_key = None
            text = None
            if enable_vision and image is not None and video is None:
                cache_key = self._image_prompt_cache_key(image, model, system_prompt, user_prompt, vision_max_size, max_tokens, temperature, identity_trigger)
                if reuse_identical_image:
                    text = _cached_llama_prompt(cache_key)
                    cache_hit = cache_hit or text is not None

            if text is None:
                if not models_unloaded:
                    comfy.model_management.unload_all_models()
                    comfy.model_management.soft_empty_cache()
                    models_unloaded = True
                user_content = [{"type": "text", "text": user_prompt}, *media_content]
                request_max_tokens = max_tokens
                for attempt in range(2):
                    payload = {
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_content if enable_vision else user_prompt},
                        ],
                        "max_tokens": request_max_tokens,
                        "temperature": temperature,
                        "seed": seed,
                        "stream": False,
                        "chat_template_kwargs": {"enable_thinking": False},
                    }
                    response = self._request_json(f"{server_url}/v1/chat/completions", payload, timeout)
                    try:
                        choice = response["choices"][0]
                        text = choice["message"]["content"]
                    except (KeyError, IndexError, TypeError) as error:
                        raise RuntimeError(f"Unexpected llama.cpp response: {response}") from error
                    if not isinstance(text, str):
                        raise RuntimeError("llama.cpp returned a non-text response")
                    text = text.strip()
                    truncated = choice.get("finish_reason") == "length"
                    repeated_layout = sheet_mode and self._repeats_character_sheet_layout(text)
                    if not truncated and not repeated_layout:
                        break
                    if attempt:
                        reason = "hit the token limit" if truncated else "repeated the layout instructions"
                        raise RuntimeError(f"llama.cpp character description {reason} twice")
                    request_max_tokens = min(8192, max(request_max_tokens * 2, 768))
                    if sheet_mode:
                        retry_instruction = "Your previous answer was invalid. Start immediately with the character's visible hair, complexion, clothing, and distinctive traits. Do not mention any board, sheet, panels, views, percentages, framing, background, or gutters."
                        if enable_vision:
                            user_content = [{"type": "text", "text": user_prompt + "\n\n" + retry_instruction}, *media_content]
                        else:
                            user_prompt += "\n\n" + retry_instruction
                if sheet_mode:
                    text = self._compose_character_sheet_prompt(text)
                text = self._enforce_body_modes(text, curvy_mode, glamorous_mode, huge_breasts_mode, curvy_strength, glamorous_strength, huge_breasts_strength)
                if sheet_mode and character_identity == "LoRA / model" and identity_trigger:
                    text = f"{identity_trigger}, {text}"
                if cache_key is not None:
                    try:
                        _cache_llama_prompt(cache_key, text)
                    except OSError as error:
                        print(f"[JH llama.cpp Prompt] Could not write prompt cache: {error}")
            results[sheet_mode] = text

        if models_unloaded:
            self._wait_for_vram_handoff(server_url, timeout)

        if cache_hit:
            self._send_cache_hit(unique_id)
        normal_prompt = results.get(False, "")
        character_sheet_prompt = results.get(True, "")
        prompts = [results[sheet_mode] for sheet_mode in sheet_modes]
        display_output = prompts[0]
        if output_mode == "Combined":
            display_output = f"Normal prompt\n\n{normal_prompt}\n\nCharacter sheet prompt\n\n{character_sheet_prompt}"
        loras = _normalize_loras(lora_info or [])
        _store_workflow_node_property(extra_pnginfo, unique_id, "jh_llama_prompt_output", display_output)
        return {"ui": {"text": [display_output], "lora_info": [json.dumps(loras, ensure_ascii=False)]}, "result": (prompts,)}


class JHPriorityPassthrough(comfy_io.ComfyNode):
    @classmethod
    def define_schema(cls):
        template = comfy_io.Autogrow.TemplateNames(
            input=comfy_io.AnyType.Input("input", optional=True),
            names=[f"input{i}" for i in range(1, 101)],
            min=1,
        )
        return comfy_io.Schema(
            node_id="JHPriorityPassthrough",
            display_name="JH Priority Passthrough",
            category=JH_UTILS_CATEGORY,
            description="Outputs the selected input, or automatically chooses an available input when selector is 0.",
            inputs=[
                comfy_io.Autogrow.Input("inputs", template=template, optional=True),
                comfy_io.Boolean.Input("random_mode", display_name="random", default=False, label_on="On", label_off="Off", socketless=True),
                comfy_io.String.Input("bypassed_inputs", default="", socketless=True),
                comfy_io.Int.Input("selected_input", display_name="selector", default=0, min=0, max=100, step=1, socketless=True, tooltip="0: automatic priority/random selection. 1-100: select input1-input100."),
            ],
            outputs=[comfy_io.AnyType.Output("output")],
        )

    @classmethod
    def fingerprint_inputs(cls, random_mode=False, selected_input=0, **kwargs):
        return random.random() if random_mode and selected_input == 0 else False

    @staticmethod
    def _format_value(value):
        if isinstance(value, str):
            return repr(value if len(value) <= 200 else f"{value[:197]}...")
        if value is None or isinstance(value, (bool, int, float)):
            return repr(value)
        if isinstance(value, torch.Tensor):
            return f"Tensor(shape={tuple(value.shape)}, dtype={value.dtype}, device={value.device})"
        if isinstance(value, np.ndarray):
            return f"ndarray(shape={value.shape}, dtype={value.dtype})"
        if isinstance(value, dict):
            keys = ", ".join(str(key) for key in list(value)[:8])
            suffix = ", ..." if len(value) > 8 else ""
            return f"dict(keys=[{keys}{suffix}])"
        if isinstance(value, (list, tuple)):
            return f"{type(value).__name__}(len={len(value)})"
        return type(value).__name__

    @classmethod
    def execute(cls, inputs, random_mode=False, bypassed_inputs="", selected_input=0):
        try:
            bypassed = set(json.loads(bypassed_inputs)) if bypassed_inputs else set()
        except (json.JSONDecodeError, TypeError):
            bypassed = set()
        available = [(name, value) for name, value in inputs.items() if name not in bypassed]
        if not available:
            names = list(inputs) or sorted(bypassed)
            info = [f"{name}: bypassed (blocked)" for name in names]
            if not info:
                info = ["No available input (blocked)"]
            return comfy_io.NodeOutput(ExecutionBlocker(None), ui={"priority_info": info})
        if selected_input:
            selected_name = f"input{selected_input}"
            if selected_name in bypassed:
                info = [f"{name}: bypassed (blocked)" if name == selected_name else f"{name}: {cls._format_value(value)}" for name, value in inputs.items()]
                if selected_name not in inputs:
                    info.append(f"{selected_name}: bypassed (blocked)")
                return comfy_io.NodeOutput(ExecutionBlocker(None), ui={"priority_info": info})
            if selected_name not in inputs:
                raise ValueError(f"Selected {selected_name} is not connected.")
            output = inputs[selected_name]
        else:
            selected_name, output = random.choice(available) if random_mode else available[0]
        info = [f"{name}: bypassed (ignored)" if name in bypassed else f"{name}: {cls._format_value(value)}" for name, value in inputs.items()]
        info.append(f"output ({selected_name}): {cls._format_value(output)}")
        return comfy_io.NodeOutput(output, ui={"priority_info": info})


class JHImagePreview(PreviewImage):
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {"image": ("IMAGE",)},
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ()
    FUNCTION = "preview_image"
    OUTPUT_NODE = True
    CATEGORY = JH_IMAGE_CATEGORY

    def preview_image(self, image, prompt=None, extra_pnginfo=None):
        result = self.save_images(image, "jh_preview", prompt, extra_pnginfo)
        return {"ui": result["ui"]}


NODE_CLASS_MAPPINGS = {
    "SaveImageToNAS": SaveImageToNAS,
    "SaveVideoToNAS": SaveVideoToNAS,
    "CalculateCropWindow": CalculateCropWindow,
    "AspectRatioCalculator": AspectRatioCalculator,
    "ResolutionDurationCalculator": ResolutionDurationCalculator,
    "ExtractStartEndFrames": ExtractStartEndFrames,
    "LoadImageFromPath": LoadImageFromPath,
    "ImageGridWithCaptions": ImageGridWithCaptions,
    "JHClipboardText": JHClipboardText,
    "FluxClipboardText": JHClipboardText,
    "JHPromptBuilder": JHPromptBuilder,
    "JHClipboardImage": JHClipboardImage,
    "JHCivitaiImage": JHCivitaiImage,
    "JHAutoImageFeed": JHAutoImageFeed,
    "JHBrowserSessionSetup": JHBrowserSessionSetup,
    "JHLoraLoader": JHLoraLoader,
    "JHShowText": JHShowText,
    "JHSavedPicker": JHSavedPicker,
    "JHSavedTextPicker": JHSavedTextPicker,
    "JHSavedTextLoraPicker": JHSavedTextLoraPicker,
    "JHLlamaPrompt": JHLlamaPrompt,
    "JHPriorityPassthrough": JHPriorityPassthrough,
    "JHImagePreview": JHImagePreview,
    "JHLoadImageMask": JHLoadImageMask,
    "JHLoadVideoMask": JHLoadVideoMask,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SaveImageToNAS": "JH Save Image to NAS",
    "SaveVideoToNAS": "JH Save Video to NAS",
    "CalculateCropWindow": "JH Calculate Crop Window",
    "AspectRatioCalculator": "JH Aspect Ratio Calculator",
    "ResolutionDurationCalculator": "JH Resolution & Duration",
    "ExtractStartEndFrames": "JH Extract Start/End Frames",
    "LoadImageFromPath": "JH Load Image from Path",
    "ImageGridWithCaptions": "JH Image Grid with Captions",
    "JHClipboardText": "JH Text Clipboard",
    "FluxClipboardText": "JH Text Clipboard (Legacy)",
    "JHPromptBuilder": "JH Prompt Builder",
    "JHClipboardImage": "JH Image Clipboard",
    "JHCivitaiImage": "JH Civitai Image",
    "JHAutoImageFeed": "JH Auto Image Feed",
    "JHBrowserSessionSetup": "JH Browser Session Setup",
    "JHLoraLoader": "JH LoRA Loader & Info",
    "JHShowText": "JH Show Text",
    "JHSavedPicker": "JH Saved Picker",
    "JHSavedTextPicker": "JH Saved Picker",
    "JHSavedTextLoraPicker": "JH Saved Picker",
    "JHLlamaPrompt": "JH llama.cpp Prompt",
    "JHPriorityPassthrough": "JH Priority Passthrough",
    "JHImagePreview": "JH Image Preview & Copy",
    "JHLoadImageMask": "JH Load Image & Mask",
    "JHLoadVideoMask": "JH Load Video & Mask",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
