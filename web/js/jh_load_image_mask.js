import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

const MARGIN = 10;
const HANDLE = 8;
const MIN_SELECTION = 6;
const COLORS = ["#49a7ff", "#ff6b8a", "#65d889", "#d991ff", "#ffc857", "#4dd9d0"];

function toast(severity, summary, detail) {
    app.extensionManager?.toast?.add?.({ severity, summary, detail, life: 3000 });
}

function hotkeyFromEvent(event) {
    if (["Control", "Alt", "Shift", "Meta"].includes(event.key)) return "";
    const names = { " ": "Space", Escape: "Esc", ArrowUp: "Up", ArrowDown: "Down", ArrowLeft: "Left", ArrowRight: "Right" };
    let key = names[event.key] || event.key;
    if (key.length === 1) key = key.toUpperCase();
    return [event.ctrlKey && "Ctrl", event.altKey && "Alt", event.shiftKey && "Shift", event.metaKey && "Meta", key].filter(Boolean).join("+");
}

let recordingHotkey = null;

function startHotkeyRecording(node, widget, action) {
    if (recordingHotkey) recordingHotkey.widget.recording = null;
    recordingHotkey = { node, widget, action };
    globalThis.jhExternalHotkeyRecording = true;
    widget.recording = action;
    node.setDirtyCanvas?.(true, false);
    toast("info", "Shortcut", "Press a shortcut. Delete clears it; Esc cancels.");
}

window.addEventListener("keydown", (event) => {
    if (!recordingHotkey) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (event.key === "Escape") {
        recordingHotkey.widget.recording = null;
        recordingHotkey.node.setDirtyCanvas?.(true, false);
        recordingHotkey = null;
        globalThis.jhExternalHotkeyRecording = false;
        return;
    }
    const value = event.key === "Delete" || event.key === "Backspace" ? "" : hotkeyFromEvent(event);
    if (!value && event.key !== "Delete" && event.key !== "Backspace") return;
    const { node, widget, action } = recordingHotkey;
    widget.hotkeys[action] = value;
    widget.recording = null;
    node.properties = node.properties || {};
    node.properties.jh_load_image_mask_hotkeys = { ...widget.hotkeys };
    node.graph?.change?.();
    node.setDirtyCanvas?.(true, false);
    recordingHotkey = null;
    globalThis.jhExternalHotkeyRecording = false;
}, true);

async function normalizeImageBlobToPng(blob) {
    if (blob.type === "image/png") return blob;
    const bitmap = await createImageBitmap(blob);
    const canvas = document.createElement("canvas");
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    canvas.getContext("2d").drawImage(bitmap, 0, 0);
    bitmap.close?.();
    return new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
}

async function readClipboardImage() {
    for (const item of await navigator.clipboard.read()) {
        const type = item.types.find((candidate) => candidate.startsWith("image/"));
        if (type) return item.getType(type);
    }
    return null;
}

async function uploadClipboardImage(blob) {
    const png = await normalizeImageBlobToPng(blob);
    const stamp = new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14);
    const file = new File([png], `jh_clipboard_${stamp}_${Math.random().toString(36).slice(2, 8)}.png`, { type: "image/png" });
    const form = new FormData();
    form.append("image", file);
    form.append("type", "input");
    form.append("subfolder", "");
    form.append("overwrite", "false");
    const response = await api.fetchApi("/upload/image", { method: "POST", body: form });
    if (!response.ok) throw new Error(`Image upload failed: ${response.status}`);
    const data = await response.json();
    return [data.subfolder, data.name].filter(Boolean).join("/");
}

async function copyImageValue(value) {
    const info = parseImageValue(value);
    if (!info) throw new Error("No image selected");
    const params = new URLSearchParams({ filename: info.filename, type: info.type, subfolder: info.subfolder, rand: Math.random().toString() });
    const response = await fetch(api.apiURL(`/view?${params}`), { cache: "no-store" });
    if (!response.ok) throw new Error(`Image fetch failed: ${response.status}`);
    const png = await normalizeImageBlobToPng(await response.blob());
    await navigator.clipboard.write([new ClipboardItem({ "image/png": png })]);
}

function makeImageClipboardActions(onPaste, onPasteAndRun, onCopy) {
    const actions = [
        { id: "run", label: "PASTE & RUN", color: "#176b87", hover: "#2187a8", run: onPasteAndRun },
        { id: "paste", label: "PASTE ONLY", color: "#3b4654", hover: "#526173", run: onPaste },
        { id: "copy", label: "COPY", color: "#3b4654", hover: "#526173", run: onCopy },
        { id: "runHotkey", action: "run", color: "#40334d", hover: "#59436d" },
        { id: "pasteHotkey", action: "paste", color: "#40334d", hover: "#59436d" },
    ];
    return {
        type: "jh_load_image_clipboard_actions", name: "jh_load_image_clipboard_actions",
        serialize: false, options: { serialize: false }, hotkeys: { run: "Alt+Shift+V", paste: "Alt+V" },
        recording: null, pressed: null, hovered: null, bounds: {},
        computeSize(width) { return [width || 0, 56]; },
        draw(ctx, _node, width, y) {
            const margin = 15;
            const gap = 6;
            const innerWidth = width - margin * 2;
            const runWidth = Math.floor((innerWidth - gap * 2) * 0.43);
            const pasteWidth = Math.floor((innerWidth - gap * 2) * 0.34);
            const copyWidth = innerWidth - gap * 2 - runWidth - pasteWidth;
            const hotkeyWidth = Math.floor((innerWidth - gap) / 2);
            this.bounds.run = [margin, y, runWidth, 26];
            this.bounds.paste = [margin + runWidth + gap, y, pasteWidth, 26];
            this.bounds.copy = [margin + runWidth + gap + pasteWidth + gap, y, copyWidth, 26];
            this.bounds.runHotkey = [margin, y + 34, hotkeyWidth, 18];
            this.bounds.pasteHotkey = [margin + hotkeyWidth + gap, y + 34, innerWidth - hotkeyWidth - gap, 18];
            ctx.save();
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            for (const action of actions) {
                const [x, buttonY, buttonWidth, buttonHeight] = this.bounds[action.id];
                ctx.fillStyle = this.hovered === action.id ? action.hover : action.color;
                ctx.globalAlpha = this.pressed === action.id ? 0.72 : 1;
                ctx.beginPath();
                ctx.roundRect(x, buttonY, buttonWidth, buttonHeight, 5);
                ctx.fill();
                ctx.globalAlpha = 1;
                ctx.fillStyle = action.id === "run" ? "#f4fbff" : "#d9dde2";
                ctx.font = action.action ? "600 8px sans-serif" : "600 9px sans-serif";
                const label = action.action
                    ? (this.recording === action.action ? "PRESS SHORTCUT..." : `${action.action === "run" ? "RUN" : "PASTE"}  ·  ${this.hotkeys[action.action] || "NOT SET"}`)
                    : action.label;
                ctx.fillText(label, x + buttonWidth / 2, buttonY + buttonHeight / 2 + 0.5);
            }
            ctx.restore();
        },
        mouse(event, pos, node) {
            const hit = actions.find((action) => {
                const bounds = this.bounds[action.id];
                return bounds && pos[0] >= bounds[0] && pos[0] <= bounds[0] + bounds[2] && pos[1] >= bounds[1] && pos[1] <= bounds[1] + bounds[3];
            });
            if (event.type === "pointermove") {
                this.hovered = hit?.id || null;
                node.setDirtyCanvas?.(true, false);
                return Boolean(hit || this.pressed);
            }
            if (event.type === "pointerdown" && hit) {
                this.pressed = hit.id;
                node.setDirtyCanvas?.(true, false);
                return true;
            }
            if (event.type === "pointerup") {
                const pressed = this.pressed;
                this.pressed = null;
                node.setDirtyCanvas?.(true, false);
                if (hit?.id === pressed) {
                    if (hit.action) startHotkeyRecording(node, this, hit.action);
                    else hit.run();
                }
                return Boolean(pressed);
            }
            return false;
        },
    };
}

function parseImageValue(value) {
    if (!value) return null;
    let filename = String(value);
    let type = "input";
    const annotated = filename.match(/^(.*) \[(\w+)\]$/);
    if (annotated) {
        filename = annotated[1];
        type = annotated[2];
    }
    let subfolder = "";
    const slash = filename.lastIndexOf("/");
    if (slash >= 0) {
        subfolder = filename.slice(0, slash);
        filename = filename.slice(slash + 1);
    }
    return { filename, type, subfolder };
}

function defaultSlot(model = "") {
    return {
        mode: "crop",
        operation: "add",
        rect: null,
        prompt: "",
        threshold: 0.5,
        refine_iterations: 2,
        grow_mask: 0,
        feather_mask: 0,
        individual_masks: false,
        sam2_prompt_mode: "brush",
        sam2_brush_mode: "include",
        sam2_brush_size: 24,
        sam2_positive_points: [],
        sam2_negative_points: [],
        sam2_brush_history: [],
        masking_prompt_mode: "box",
        masking_brush_mode: "include",
        masking_brush_size: 24,
        masking_brush_strokes: [],
        model,
    };
}

function usesRectangle(slot) {
    return slot?.mode === "crop" || (slot?.mode === "masking" && slot?.masking_prompt_mode !== "brush") || (slot?.mode === "sam2" && slot?.sam2_prompt_mode === "box");
}

function annotatedAction(widget) {
    if (widget.options?.reset !== undefined && widget.value !== widget.options.reset) return "reset";
    if (widget.options?.disable !== undefined && widget.value !== widget.options.disable) return "disable";
    if (widget.options?.reset !== undefined) return "no-reset";
    if (widget.options?.disable !== undefined) return "no-disable";
    return null;
}

function drawAnnotatedNumber(ctx, node, width, y, height) {
    const margin = 15;
    const colors = globalThis.LiteGraph || {};
    this._jhDrawWidth = width;
    this._jhHitAreas = {
        decrement: [margin + 6, margin + 16],
        action: [width - margin - 34, width - margin - 18],
        increment: [width - margin - 16, width - margin - 6],
    };
    ctx.save();
    ctx.strokeStyle = colors.WIDGET_OUTLINE_COLOR || "#666";
    ctx.fillStyle = colors.WIDGET_BGCOLOR || "#222";
    ctx.beginPath();
    ctx.roundRect(margin, y, width - margin * 2, height, height * 0.5);
    ctx.fill();
    ctx.stroke();

    ctx.fillStyle = colors.WIDGET_TEXT_COLOR || "#ddd";
    ctx.beginPath();
    ctx.moveTo(margin + 16, y + 5);
    ctx.lineTo(margin + 6, y + height * 0.5);
    ctx.lineTo(margin + 16, y + height - 5);
    ctx.fill();
    ctx.beginPath();
    ctx.moveTo(width - margin - 16, y + 5);
    ctx.lineTo(width - margin - 6, y + height * 0.5);
    ctx.lineTo(width - margin - 16, y + height - 5);
    ctx.fill();

    const action = annotatedAction(this);
    if (action) {
        const active = !action.startsWith("no-");
        ctx.strokeStyle = ctx.fillStyle = active
            ? (colors.WIDGET_TEXT_COLOR || "#ddd")
            : (colors.WIDGET_OUTLINE_COLOR || "#666");
        const iconX = width - margin - 26;
        const iconY = y + height * 0.5;
        ctx.beginPath();
        if (action.endsWith("reset")) {
            ctx.arc(iconX, iconY, 4, Math.PI * 1.5, Math.PI);
            ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(iconX, iconY - 1.5);
            ctx.lineTo(iconX, iconY - 6.5);
            ctx.lineTo(iconX - 4, iconY - 3.5);
            ctx.fill();
        } else {
            ctx.arc(iconX, iconY, 4, Math.PI * 2 / 3, Math.PI * 8 / 3);
            ctx.moveTo(iconX - Math.SQRT2 * 2, iconY + Math.SQRT2 * 2);
            ctx.lineTo(iconX + Math.SQRT2 * 2, iconY - Math.SQRT2 * 2);
            ctx.stroke();
        }
    }

    const valueText = Number.isInteger(Number(this.value)) ? String(this.value) : Number(this.value).toFixed(3).replace(/\.?0+$/, "");
    const valueX = width - margin - 40;
    ctx.textBaseline = "alphabetic";
    ctx.textAlign = "right";
    ctx.fillStyle = colors.WIDGET_TEXT_COLOR || "#ddd";
    ctx.fillText(valueText, valueX, y + height * 0.7);
    const annotation = this.annotation?.(this.value);
    if (annotation) {
        ctx.fillStyle = colors.WIDGET_OUTLINE_COLOR || "#777";
        ctx.fillText(annotation, valueX - ctx.measureText(valueText).width - 6, y + height * 0.7);
    }
    ctx.textAlign = "left";
    ctx.fillStyle = colors.WIDGET_SECONDARY_TEXT_COLOR || "#aaa";
    ctx.fillText(this.label || this.name, margin * 2 + 5, y + height * 0.7);
    ctx.restore();
}

function mouseAnnotatedNumber(event, position, node) {
    const width = this._jhDrawWidth || node.size[0];
    const eventX = Number(event.offsetX);
    const x = this.y === 0 && Number.isFinite(eventX) ? eventX : position[0];
    const type = event.type.replace("mouse", "pointer");
    const hitAreas = this._jhHitAreas || {
        decrement: [21, 31],
        action: [width - 49, width - 33],
        increment: [width - 31, width - 21],
    };
    const inArea = ([left, right]) => x >= left && x <= right;
    const iconButton = inArea(hitAreas.action);
    const leftButton = inArea(hitAreas.decrement);
    const rightButton = inArea(hitAreas.increment);

    const setValue = (value) => {
        const oldValue = this.value;
        this.value = value;
        if (this.options.min != null) this.value = Math.max(this.options.min, this.value);
        if (this.options.max != null) this.value = Math.min(this.options.max, this.value);
        if (oldValue === this.value) return;
        this.callback?.(this.value, app.canvas, node, event);
        node.graph?.change?.();
        node.setDirtyCanvas(true, true);
    };

    if (type === "pointermove") {
        if (!this._jhButtonPress && event.deltaX) setValue(this.value + event.deltaX * (this.options.step || 1));
        return true;
    }
    if (type === "pointerdown" && iconButton) {
        this._jhButtonPress = true;
        const action = annotatedAction(this);
        if (action === "reset") setValue(this.options.reset);
        else if (action === "disable") setValue(this.options.disable);
        return true;
    }
    if (type === "pointerdown" && (leftButton || rightButton)) {
        this._jhButtonPress = true;
        setValue(this.value + (rightButton ? 1 : -1) * (this.options.step || 1));
        return true;
    }
    if (type === "pointerdown") {
        this._jhButtonPress = false;
        return true;
    }
    if (type === "pointerup" && this._jhButtonPress) {
        this._jhButtonPress = false;
        return true;
    }
    if (type === "pointerup" && !iconButton && !leftButton && !rightButton && event.click_time < 200) {
        app.canvas.prompt("Value", this.value, (value) => {
            const parsed = Number(value);
            if (Number.isFinite(parsed)) setValue(parsed);
        }, event);
        return true;
    }
    return true;
}

app.registerExtension({
    name: "jh.load_image_mask",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (!["JHLoadImageMask", "JHLoadVideoMask"].includes(nodeData.name)) return;
        const isVideo = nodeData.name === "JHLoadVideoMask";

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = onNodeCreated?.apply(this, arguments);
            const node = this;
            const imageWidget = node.widgets.find((widget) => widget.name === (isVideo ? "video" : "image"));
            const configWidget = node.widgets.find((widget) => widget.name === "slot_config");
            const megapixelsWidget = node.widgets.find((widget) => widget.name === "max_megapixels");
            const selectionWidget = node.widgets.find((widget) => widget.name === "selection_mode");
            const modelWidget = node.widgets.find((widget) => widget.name === "sam3_model");
            const forceRateWidget = node.widgets.find((widget) => widget.name === "force_rate");
            const frameLoadCapWidget = node.widgets.find((widget) => widget.name === "frame_load_cap");
            const skipFirstFramesWidget = node.widgets.find((widget) => widget.name === "skip_first_frames");
            for (const widget of [configWidget, modelWidget]) {
                widget.hidden = true;
                widget.options = widget.options || {};
                widget.options.hidden = true;
            }

            Object.defineProperty(node, "imgs", { get: () => undefined, set: () => {} });
            if (isVideo) {
                Object.defineProperty(node, "videos", { get: () => undefined, set: () => {} });
                const addDOMWidget = node.addDOMWidget.bind(node);
                node.addDOMWidget = function (name) {
                    const widget = addDOMWidget(...arguments);
                    if (name === "video-preview" || name === "$$comfy_animation_preview") {
                        widget?.onRemove?.();
                        const index = node.widgets.indexOf(widget);
                        if (index >= 0) node.widgets.splice(index, 1);
                    }
                    return widget;
                };
                for (let index = node.widgets.length - 1; index >= 0; index--) {
                    if (!["video-preview", "$$comfy_animation_preview"].includes(node.widgets[index].name)) continue;
                    node.widgets[index].onRemove?.();
                    node.widgets.splice(index, 1);
                }
            }

            const modelValues = Array.isArray(modelWidget.options?.values)
                ? modelWidget.options.values
                : [modelWidget.value].filter(Boolean);
            const sam2ModelValues = modelValues.filter((value) => String(value).startsWith("sam2/"));
            const sam3ModelValues = modelValues.filter((value) => !String(value).startsWith("sam2/"));
            const modelForMode = (mode, value = "") => {
                const values = mode === "sam2" ? sam2ModelValues : sam3ModelValues;
                return values.includes(value) ? value : (values[0] || "");
            };
            const state = {
                img: null, video: null, box: null, drag: null, active: 0, slots: [],
                fps: 0, frameCount: 0, availableFrameCount: 0, sourceFps: 0, sourceFrameCount: 0, sourceDuration: 0, startTime: 0, endTime: 0,
            };

            let clipboardActions = null;
            if (!isVideo) {
                const pasteImage = async () => {
                    try {
                        const blob = await readClipboardImage();
                        if (!blob) {
                            toast("warn", "Clipboard", "No image was found in the clipboard.");
                            return false;
                        }
                        const value = await uploadClipboardImage(blob);
                        const values = imageWidget.options?.values;
                        if (Array.isArray(values) && !values.includes(value)) values.push(value);
                        imageWidget.value = value;
                        imageWidget.callback?.(value);
                        return true;
                    } catch (error) {
                        console.error("[JH Load Image & Mask] Clipboard paste failed:", error);
                        toast("warn", "Clipboard", "Clipboard image could not be read.");
                        return false;
                    }
                };
                const pasteAndRun = async () => {
                    if (node.jhClipboardBusy) return;
                    node.jhClipboardBusy = true;
                    try {
                        if (await pasteImage()) await app.queuePrompt(0, 1);
                    } catch (error) {
                        console.error("[JH Load Image & Mask] Queue failed:", error);
                        toast("warn", "JH Load Image & Mask", "The image was pasted, but the workflow could not be queued.");
                    } finally {
                        node.jhClipboardBusy = false;
                    }
                };
                const copyImage = async () => {
                    try {
                        await copyImageValue(imageWidget.value);
                        toast("success", "Clipboard", "Image copied.");
                    } catch (error) {
                        console.error("[JH Load Image & Mask] Clipboard copy failed:", error);
                        toast("warn", "Clipboard", imageWidget.value ? "Image could not be copied." : "There is no image to copy.");
                    }
                };
                clipboardActions = node.addCustomWidget(makeImageClipboardActions(pasteImage, pasteAndRun, copyImage));
                node.jhHotkeyBindings = () => [
                    { value: clipboardActions.hotkeys.run, run: pasteAndRun },
                    { value: clipboardActions.hotkeys.paste, run: pasteImage },
                ];
            }
            if (isVideo) {
                for (const widget of [forceRateWidget, frameLoadCapWidget, skipFirstFramesWidget]) {
                    widget.draw = drawAnnotatedNumber;
                    widget.mouse = mouseAnnotatedNumber;
                    widget.options = { ...(widget.options || {}), reset: 0, disable: 0 };
                }
                forceRateWidget.annotation = (value) => Number(value) === 0 && state.sourceFps
                    ? `${Number(state.sourceFps.toFixed(3))}←`
                    : "";
                frameLoadCapWidget.annotation = (value) => Number(value) === 0 && state.frameCount
                    ? `${state.frameCount}←`
                    : "";
                skipFirstFramesWidget.annotation = () => state.sourceFps ? `#${state.availableFrameCount}` : "";
            }
            const mediaWidth = () => state.img?.videoWidth || state.img?.naturalWidth || state.img?.width || 0;
            const mediaHeight = () => state.img?.videoHeight || state.img?.naturalHeight || state.img?.height || 0;
            let timelineInput = null;
            let frameLabel = null;
            let playerHost = null;
            let resultCanvas = null;
            let maskCanvas = null;
            let maskOverlayCanvas = null;
            let playButton = null;
            let previewStatus = null;
            let resultRenderPending = false;
            let playerResizeObserver = null;

            function persistWidgetValue(widget, value) {
                const index = node.widgets.findIndex((candidate) => candidate.name === widget.name);
                if (index < 0) return;
                node.widgets_values = Array.isArray(node.widgets_values) ? node.widgets_values : [];
                node.widgets_values[index] = value;
            }

            function readConfig() {
                try {
                    const stored = node.properties?.jh_load_image_mask_slots || configWidget.value || "{}";
                    const saved = JSON.parse(stored);
                    state.slots = Array.isArray(saved.slots) && saved.slots.length
                        ? saved.slots.map((slot) => ({
                            ...defaultSlot(modelWidget.value),
                            ...slot,
                            sam2_prompt_mode: slot.mode === "sam2" && slot.sam2_prompt_mode == null && slot.rect ? "box" : (slot.sam2_prompt_mode || "brush"),
                        }))
                        : [defaultSlot(modelWidget.value)];
                } catch (_) {
                    state.slots = [defaultSlot(modelWidget.value)];
                }
                state.active = Math.min(state.active, state.slots.length - 1);
            }

            function syncConfig() {
                configWidget.value = JSON.stringify({ version: 1, slots: state.slots });
                node.properties = node.properties || {};
                node.properties.jh_load_image_mask_slots = configWidget.value;
                node.properties.jh_load_image_mask_image = String(imageWidget.value || "");
                const maxMegapixels = Number(megapixelsWidget.value);
                node.properties.jh_load_image_mask_max_megapixels = Number.isFinite(maxMegapixels) ? maxMegapixels : 0;
                node.properties.jh_load_image_mask_selection_mode = selectionWidget.value;
                node.properties.jh_load_image_mask_model = String(modelWidget.value || "");
                persistWidgetValue(imageWidget, imageWidget.value);
                persistWidgetValue(configWidget, configWidget.value);
                persistWidgetValue(megapixelsWidget, node.properties.jh_load_image_mask_max_megapixels);
                persistWidgetValue(selectionWidget, selectionWidget.value);
                persistWidgetValue(modelWidget, modelWidget.value);
                node.setDirtyCanvas(true, true);
                renderResultPreview();
            }

            readConfig();
            syncConfig();

            function activeRect() {
                const slot = state.slots[state.active];
                return usesRectangle(slot) ? slot.rect : null;
            }

            function hitTest(px, py) {
                const rect = activeRect();
                if (!rect || !state.box) return { mode: "new" };
                const { x, y, w, h } = state.box;
                const sx = x + rect.x * w;
                const sy = y + rect.y * h;
                const sw = rect.w * w;
                const sh = rect.h * h;
                const corners = { nw: [sx, sy], ne: [sx + sw, sy], sw: [sx, sy + sh], se: [sx + sw, sy + sh] };
                for (const [corner, [cx, cy]] of Object.entries(corners)) {
                    if (Math.abs(px - cx) <= HANDLE && Math.abs(py - cy) <= HANDLE) return { mode: "resize", corner };
                }
                if (px >= sx && px <= sx + sw && py >= sy && py <= sy + sh) {
                    return { mode: "move", offX: px - sx, offY: py - sy };
                }
                return { mode: "new" };
            }

            const editor = {
                name: "jh_mask_editor",
                type: "jh_mask_editor",
                serialize: false,
                options: { serialize: false },
                computeLayoutSize(layoutNode) {
                    const innerWidth = Math.max(180, layoutNode.size[0] - MARGIN * 2);
                    const height = state.img ? innerWidth * mediaHeight() / mediaWidth() : 180;
                    return { minHeight: Math.max(140, Math.min(520, height)) + 24, minWidth: 0 };
                },
                draw(ctx, _node, width, y, height, lowQuality) {
                    const x = MARGIN;
                    const w = Math.max(1, width - MARGIN * 2);
                    const h = Math.max(1, (this.computedHeight ?? height) - 24);
                    ctx.save();
                    ctx.fillStyle = "#111";
                    ctx.fillRect(x, y, w, h);
                    if (!state.img) {
                        ctx.fillStyle = "#888";
                        ctx.textAlign = "center";
                        ctx.textBaseline = "middle";
                        ctx.fillText("no image", x + w / 2, y + h / 2);
                        ctx.restore();
                        return;
                    }

                    const sourceWidth = mediaWidth();
                    const sourceHeight = mediaHeight();
                    const scale = Math.min(w / sourceWidth, h / sourceHeight);
                    const bw = sourceWidth * scale;
                    const bh = sourceHeight * scale;
                    const bx = x + (w - bw) / 2;
                    const by = y + (h - bh) / 2;
                    state.box = { x: bx, y: by, w: bw, h: bh };
                    ctx.drawImage(state.img, bx, by, bw, bh);

                    if (!lowQuality) {
                        state.slots.forEach((slot, index) => {
                            const color = COLORS[index % COLORS.length];
                            if (slot.rect && usesRectangle(slot)) {
                                const sx = bx + slot.rect.x * bw;
                                const sy = by + slot.rect.y * bh;
                                const sw = slot.rect.w * bw;
                                const sh = slot.rect.h * bh;
                                if (slot.mode === "crop") {
                                    ctx.beginPath();
                                    ctx.rect(bx, by, bw, bh);
                                    ctx.rect(sx, sy, sw, sh);
                                    ctx.fillStyle = "rgba(0,0,0,0.5)";
                                    ctx.fill("evenodd");
                                } else {
                                    ctx.fillStyle = `${color}45`;
                                    ctx.fillRect(sx, sy, sw, sh);
                                }
                                ctx.strokeStyle = color;
                                ctx.lineWidth = index === state.active ? 2 : 1;
                                ctx.strokeRect(sx, sy, sw, sh);
                                ctx.fillStyle = color;
                                ctx.font = "11px sans-serif";
                                ctx.fillText(`${index + 1} ${slot.mode.toUpperCase()}`, sx + 4, sy + 13);
                                if (index === state.active) {
                                    for (const [hx, hy] of [[sx, sy], [sx + sw, sy], [sx, sy + sh], [sx + sw, sy + sh]]) {
                                        ctx.fillRect(hx - 3, hy - 3, 6, 6);
                                    }
                                }
                            }
                            if (slot.mode === "sam2" && slot.sam2_prompt_mode !== "box") {
                                const radius = Math.max(2, Number(slot.sam2_brush_size || 24) * scale * 0.5);
                                for (const [points, fill] of [
                                    [slot.sam2_positive_points || [], "rgba(65, 220, 110, 0.65)"],
                                    [slot.sam2_negative_points || [], "rgba(255, 75, 75, 0.65)"],
                                ]) {
                                    ctx.fillStyle = fill;
                                    for (const point of points) {
                                        ctx.beginPath();
                                        ctx.arc(bx + point.x * bw, by + point.y * bh, radius, 0, Math.PI * 2);
                                        ctx.fill();
                                    }
                                }
                            }
                            if (slot.mode === "masking" && slot.masking_prompt_mode === "brush") {
                                for (const stroke of slot.masking_brush_strokes || []) {
                                    const radius = Math.max(2, Number(stroke.size || 24) * scale * 0.5);
                                    ctx.fillStyle = stroke.mode === "exclude" ? "rgba(255, 75, 75, 0.65)" : "rgba(65, 220, 110, 0.65)";
                                    for (const point of stroke.points || []) {
                                        ctx.beginPath();
                                        ctx.arc(bx + point.x * bw, by + point.y * bh, radius, 0, Math.PI * 2);
                                        ctx.fill();
                                    }
                                }
                            }
                        });
                    }
                    ctx.fillStyle = "#aaa";
                    ctx.textAlign = "center";
                    const cropSize = previewCropSize();
                    const sizeLabel = cropSize
                        ? `Original ${sourceWidth} x ${sourceHeight}  |  Crop ${cropSize.width} x ${cropSize.height}`
                        : `${sourceWidth} x ${sourceHeight}`;
                    ctx.fillText(sizeLabel, x + w / 2, y + h + 17);
                    ctx.restore();
                },
                mouse(event, pos) {
                    let slot = state.slots[state.active];
                    if (!slot || !state.img || !state.box) return false;
                    const px = pos[0];
                    const py = pos[1];
                    const { x, y, w, h } = state.box;
                    const clampX = (value) => Math.max(x, Math.min(x + w, value));
                    const clampY = (value) => Math.max(y, Math.min(y + h, value));
                    const addSam2BrushPoint = () => {
                        const field = state.drag?.field || (slot.sam2_brush_mode === "exclude" ? "sam2_negative_points" : "sam2_positive_points");
                        const points = Array.isArray(slot[field]) ? slot[field] : (slot[field] = []);
                        const point = { x: (clampX(px) - x) / w, y: (clampY(py) - y) / h };
                        const last = points[points.length - 1];
                        const minDistance = Math.max(2, Number(slot.sam2_brush_size || 24) * w / mediaWidth() * 0.35);
                        if (!last || Math.hypot((point.x - last.x) * w, (point.y - last.y) * h) >= minDistance) {
                            points.push(point);
                            if (points.length > 256) points.splice(0, points.length - 256);
                        }
                    };
                    const addMaskingBrushPoint = () => {
                        const stroke = state.drag?.stroke;
                        if (!stroke) return;
                        const point = { x: (clampX(px) - x) / w, y: (clampY(py) - y) / h };
                        const last = stroke.points[stroke.points.length - 1];
                        const minDistance = Math.max(2, Number(stroke.size || 24) * w / mediaWidth() * 0.35);
                        if (!last || Math.hypot((point.x - last.x) * w, (point.y - last.y) * h) >= minDistance) {
                            stroke.points.push(point);
                            if (stroke.points.length > 256) stroke.points.splice(0, stroke.points.length - 256);
                        }
                    };
                    if (event.type === "pointerdown" || event.type === "mousedown") {
                        if (px < x || px > x + w || py < y || py > y + h) return false;
                        if (isVideo && state.video && !state.video.paused) state.video.pause();
                        if (slot.mode === "masking" && slot.masking_prompt_mode === "brush") {
                            slot.masking_brush_strokes = Array.isArray(slot.masking_brush_strokes) ? slot.masking_brush_strokes : [];
                            const stroke = {
                                mode: slot.masking_brush_mode === "exclude" ? "exclude" : "include",
                                size: Math.max(2, Math.round(Number(slot.masking_brush_size || 24))),
                                points: [],
                            };
                            slot.masking_brush_strokes.push(stroke);
                            if (slot.masking_brush_strokes.length > 128) slot.masking_brush_strokes.shift();
                            state.drag = { mode: "masking_brush", stroke, startX: px, startY: py, moved: false };
                            addMaskingBrushPoint();
                            node.setDirtyCanvas(true, true);
                            return true;
                        }
                        if (slot.mode === "sam2" && slot.sam2_prompt_mode !== "box") {
                            const field = slot.sam2_brush_mode === "exclude" ? "sam2_negative_points" : "sam2_positive_points";
                            const points = Array.isArray(slot[field]) ? slot[field] : (slot[field] = []);
                            state.drag = { mode: "sam2_brush", field, startLength: points.length, startX: px, startY: py, moved: false };
                            addSam2BrushPoint();
                            node.setDirtyCanvas(true, true);
                            return true;
                        }
                        if (!usesRectangle(slot) || slot.rect) {
                            for (let index = state.slots.length - 1; index >= 0; index--) {
                                const candidate = state.slots[index];
                                if (!candidate.rect || !usesRectangle(candidate)) continue;
                                const sx = x + candidate.rect.x * w;
                                const sy = y + candidate.rect.y * h;
                                if (px >= sx && px <= sx + candidate.rect.w * w && py >= sy && py <= sy + candidate.rect.h * h) {
                                    state.active = index;
                                    slot = candidate;
                                    break;
                                }
                            }
                        }
                        if (!usesRectangle(slot)) {
                            const cropIndex = state.slots.findLastIndex((candidate) => candidate.mode === "crop");
                            if (cropIndex >= 0) {
                                state.active = cropIndex;
                                slot = state.slots[cropIndex];
                            }
                        }
                        if (!usesRectangle(slot)) return false;
                        state.drag = { ...hitTest(px, py), startX: px, startY: py, moved: false };
                        node.setDirtyCanvas(true, true);
                        return true;
                    }
                    if (!state.drag) return false;
                    if (event.type === "pointermove" || event.type === "mousemove") {
                        const drag = state.drag;
                        if (Math.abs(px - drag.startX) + Math.abs(py - drag.startY) > 2) drag.moved = true;
                        if (drag.mode === "sam2_brush") {
                            addSam2BrushPoint();
                        } else if (drag.mode === "masking_brush") {
                            addMaskingBrushPoint();
                        } else if (drag.mode === "new") {
                            const x0 = clampX(Math.min(drag.startX, px));
                            const y0 = clampY(Math.min(drag.startY, py));
                            const x1 = clampX(Math.max(drag.startX, px));
                            const y1 = clampY(Math.max(drag.startY, py));
                            if (x1 - x0 >= MIN_SELECTION && y1 - y0 >= MIN_SELECTION) {
                                slot.rect = { x: (x0 - x) / w, y: (y0 - y) / h, w: (x1 - x0) / w, h: (y1 - y0) / h };
                            }
                        } else if (drag.mode === "move" && slot.rect) {
                            slot.rect.x = Math.max(0, Math.min(1 - slot.rect.w, (clampX(px - drag.offX) - x) / w));
                            slot.rect.y = Math.max(0, Math.min(1 - slot.rect.h, (clampY(py - drag.offY) - y) / h));
                        } else if (drag.mode === "resize" && slot.rect) {
                            let x0 = x + slot.rect.x * w;
                            let y0 = y + slot.rect.y * h;
                            let x1 = x0 + slot.rect.w * w;
                            let y1 = y0 + slot.rect.h * h;
                            if (drag.corner.includes("w")) x0 = clampX(px);
                            if (drag.corner.includes("e")) x1 = clampX(px);
                            if (drag.corner.includes("n")) y0 = clampY(py);
                            if (drag.corner.includes("s")) y1 = clampY(py);
                            if (Math.abs(x1 - x0) >= MIN_SELECTION && Math.abs(y1 - y0) >= MIN_SELECTION) {
                                slot.rect = {
                                    x: (Math.min(x0, x1) - x) / w,
                                    y: (Math.min(y0, y1) - y) / h,
                                    w: Math.abs(x1 - x0) / w,
                                    h: Math.abs(y1 - y0) / h,
                                };
                            }
                        }
                        this.triggerDraw?.();
                        renderResultPreview();
                        return true;
                    }
                    if (event.type === "pointerup" || event.type === "mouseup") {
                        if (state.drag.mode === "new" && !state.drag.moved) slot.rect = null;
                        if (state.drag.mode === "sam2_brush") {
                            const count = (slot[state.drag.field]?.length || 0) - state.drag.startLength;
                            if (count > 0) {
                                slot.sam2_brush_history = Array.isArray(slot.sam2_brush_history) ? slot.sam2_brush_history : [];
                                slot.sam2_brush_history.push({ field: state.drag.field, count });
                            }
                        }
                        state.drag = null;
                        syncConfig();
                        this.triggerDraw?.();
                        if (isVideo) requestAnimationFrame(() => node.setSize([node.size[0], Math.max(node.size[1], node.computeSize()[1])]));
                        return true;
                    }
                    return false;
                },
            };

            const editorWidget = node.addCustomWidget(editor);

            function redrawEditor() {
                editorWidget.triggerDraw?.();
                node.setDirtyCanvas(true, true);
            }

            function currentFrame(mediaTime = state.video?.currentTime || 0) {
                if (!state.fps) return 0;
                return Math.max(0, Math.min(Math.max(0, state.frameCount - 1), Math.round((mediaTime - state.startTime) * state.fps)));
            }

            function updateVideoRange(seekToStart = false) {
                if (!state.sourceFps) return;
                const forcedRate = Number(forceRateWidget?.value) || 0;
                state.fps = forcedRate > 0 ? forcedRate : state.sourceFps;
                const skip = Math.max(0, Math.round(Number(skipFirstFramesWidget?.value) || 0));
                const cap = Math.max(0, Math.round(Number(frameLoadCapWidget?.value) || 0));
                state.startTime = skip / state.fps;
                const availableDuration = Math.max(0, state.sourceDuration - state.startTime);
                const availableFrames = forcedRate > 0
                    ? Math.max(0, Math.round(availableDuration * state.fps))
                    : Math.max(0, state.sourceFrameCount - skip);
                state.availableFrameCount = availableFrames;
                state.frameCount = cap > 0 ? Math.min(cap, availableFrames) : availableFrames;
                state.endTime = state.startTime + state.frameCount / state.fps;
                forceRateWidget.options.reset = state.sourceFps;
                frameLoadCapWidget.options.reset = availableFrames;
                skipFirstFramesWidget.options.reset = 0;
                if (state.video && (seekToStart || state.video.currentTime < state.startTime || state.video.currentTime >= state.endTime)) {
                    state.video.pause();
                    state.video.currentTime = Math.min(state.video.duration || state.startTime, state.startTime + 0.001);
                }
                updateVideoFrame();
            }

            function previewCropRect() {
                let x0 = 1;
                let y0 = 1;
                let x1 = 0;
                let y1 = 0;
                let found = false;
                for (const slot of state.slots) {
                    if (slot?.enabled === false || slot?.mode !== "crop" || !slot.rect) continue;
                    x0 = Math.min(x0, slot.rect.x);
                    y0 = Math.min(y0, slot.rect.y);
                    x1 = Math.max(x1, slot.rect.x + slot.rect.w);
                    y1 = Math.max(y1, slot.rect.y + slot.rect.h);
                    found = true;
                }
                return found
                    ? { x: Math.max(0, x0), y: Math.max(0, y0), w: Math.min(1, x1) - Math.max(0, x0), h: Math.min(1, y1) - Math.max(0, y0) }
                    : { x: 0, y: 0, w: 1, h: 1 };
            }

            function previewCropSize() {
                const hasCrop = state.slots.some((slot) => slot?.enabled !== false && slot?.mode === "crop" && slot.rect);
                if (!hasCrop) return null;
                const width = mediaWidth();
                const height = mediaHeight();
                const crop = previewCropRect();
                const x0 = Math.max(0, Math.min(width - 1, Math.round(crop.x * width)));
                const y0 = Math.max(0, Math.min(height - 1, Math.round(crop.y * height)));
                const x1 = Math.max(x0 + 1, Math.min(width, Math.round((crop.x + crop.w) * width)));
                const y1 = Math.max(y0 + 1, Math.min(height, Math.round((crop.y + crop.h) * height)));
                return { width: x1 - x0, height: y1 - y0 };
            }

            function renderResultPreview() {
                if (!isVideo || !resultCanvas || !state.video || state.video.readyState < 2 || resultRenderPending) return;
                resultRenderPending = true;
                requestAnimationFrame(() => {
                    resultRenderPending = false;
                    if (!resultCanvas || !state.video || state.video.readyState < 2) return;

                    const crop = previewCropRect();
                    if (crop.w <= 0 || crop.h <= 0) return;
                    const sourceWidth = mediaWidth();
                    const sourceHeight = mediaHeight();
                    const displayWidth = Math.max(180, Math.round(playerHost?.clientWidth || node.size[0] - MARGIN * 2));
                    const pixelRatio = Math.min(2, window.devicePixelRatio || 1);
                    const canvasWidth = Math.max(1, Math.round(displayWidth * pixelRatio));
                    const canvasHeight = Math.max(1, Math.round(canvasWidth * sourceHeight * crop.h / (sourceWidth * crop.w)));
                    if (resultCanvas.width !== canvasWidth || resultCanvas.height !== canvasHeight) {
                        resultCanvas.width = canvasWidth;
                        resultCanvas.height = canvasHeight;
                        maskCanvas.width = canvasWidth;
                        maskCanvas.height = canvasHeight;
                        maskOverlayCanvas.width = canvasWidth;
                        maskOverlayCanvas.height = canvasHeight;
                    }
                    resultCanvas.style.aspectRatio = `${canvasWidth} / ${canvasHeight}`;

                    const context = resultCanvas.getContext("2d");
                    context.clearRect(0, 0, canvasWidth, canvasHeight);
                    context.drawImage(
                        state.video,
                        crop.x * sourceWidth,
                        crop.y * sourceHeight,
                        crop.w * sourceWidth,
                        crop.h * sourceHeight,
                        0,
                        0,
                        canvasWidth,
                        canvasHeight,
                    );

                    const maskContext = maskCanvas.getContext("2d");
                    maskContext.globalCompositeOperation = "source-over";
                    maskContext.clearRect(0, 0, canvasWidth, canvasHeight);
                    let hasMask = false;
                    let hasSegmentation = false;
                    for (const slot of state.slots) {
                        if (slot?.enabled === false) continue;
                        if (slot?.mode === "sam2" || slot?.mode === "sam3") {
                            hasSegmentation = true;
                            continue;
                        }
                        if (slot?.mode === "masking" && slot.masking_prompt_mode === "brush") {
                            hasSegmentation = true;
                            continue;
                        }
                        if (slot?.mode !== "masking" || !slot.rect) continue;
                        const left = Math.max(crop.x, slot.rect.x);
                        const top = Math.max(crop.y, slot.rect.y);
                        const right = Math.min(crop.x + crop.w, slot.rect.x + slot.rect.w);
                        const bottom = Math.min(crop.y + crop.h, slot.rect.y + slot.rect.h);
                        if (right <= left || bottom <= top) continue;
                        if (hasMask) {
                            maskContext.globalCompositeOperation = {
                                subtract: "destination-out",
                                intersect: "destination-in",
                                xor: "xor",
                            }[slot.operation] || "source-over";
                        }
                        maskContext.fillStyle = "#fff";
                        maskContext.fillRect(
                            (left - crop.x) / crop.w * canvasWidth,
                            (top - crop.y) / crop.h * canvasHeight,
                            (right - left) / crop.w * canvasWidth,
                            (bottom - top) / crop.h * canvasHeight,
                        );
                        hasMask = true;
                    }

                    const overlayContext = maskOverlayCanvas.getContext("2d");
                    overlayContext.globalCompositeOperation = "source-over";
                    overlayContext.clearRect(0, 0, canvasWidth, canvasHeight);
                    if (hasMask) {
                        if (selectionWidget.value === "KEEP SELECTED") {
                            overlayContext.fillStyle = "#000";
                            overlayContext.fillRect(0, 0, canvasWidth, canvasHeight);
                            overlayContext.globalCompositeOperation = "destination-out";
                            overlayContext.drawImage(maskCanvas, 0, 0);
                        } else {
                            overlayContext.drawImage(maskCanvas, 0, 0);
                            overlayContext.globalCompositeOperation = "source-in";
                            overlayContext.fillStyle = "#000";
                            overlayContext.fillRect(0, 0, canvasWidth, canvasHeight);
                        }
                        context.drawImage(maskOverlayCanvas, 0, 0);
                    }
                    if (previewStatus) previewStatus.textContent = hasSegmentation ? "CROP/MASK preview · mask processing pending" : "Result preview";
                });
            }

            function updateVideoFrame(mediaTime) {
                if (!state.video) return;
                if (state.endTime > state.startTime && state.endTime < state.sourceDuration && state.video.currentTime >= state.endTime) {
                    state.video.pause();
                    state.video.currentTime = Math.max(state.startTime, state.endTime - 1 / state.fps);
                    return;
                }
                const frame = currentFrame(mediaTime);
                if (timelineInput) {
                    timelineInput.max = String(Math.max(0, state.frameCount - 1));
                    timelineInput.value = String(frame);
                    timelineInput.disabled = state.frameCount <= 1;
                }
                if (frameLabel) {
                    const total = state.frameCount || "?";
                    frameLabel.textContent = `Frame ${frame + 1} / ${total}  ·  ${state.video.currentTime.toFixed(3)}s`;
                }
                redrawEditor();
                renderResultPreview();
            }

            function toggleVideoPlayback() {
                const video = state.video;
                if (!video) return;
                if (!video.paused) {
                    video.pause();
                    return;
                }
                const playbackEnd = state.endTime > state.startTime
                    ? Math.min(state.endTime, video.duration || state.endTime)
                    : video.duration;
                const endTolerance = state.fps > 0 ? 0.5 / state.fps : 0.02;
                const atLastFrame = state.frameCount > 0 && currentFrame() >= state.frameCount - 1;
                if (video.ended || atLastFrame || (Number.isFinite(playbackEnd) && video.currentTime >= playbackEnd - endTolerance)) {
                    video.currentTime = state.startTime;
                }
                video.play().catch(() => {});
            }

            if (isVideo) {
                const panel = document.createElement("div");
                Object.assign(panel.style, {
                    display: "grid",
                    gridTemplateRows: "auto auto minmax(120px, 1fr) auto",
                    gap: "6px",
                    padding: "4px 10px 8px",
                    boxSizing: "border-box",
                    width: "100%",
                    height: "100%",
                    minHeight: "0",
                });

                const timelineHeader = document.createElement("div");
                Object.assign(timelineHeader.style, {
                    display: "flex",
                    justifyContent: "space-between",
                    color: "#bbb",
                    font: "12px sans-serif",
                });
                const timelineTitle = document.createElement("span");
                timelineTitle.textContent = "Selection frame";
                frameLabel = document.createElement("span");
                frameLabel.textContent = "Frame 1 / ?";
                timelineHeader.append(timelineTitle, frameLabel);

                timelineInput = document.createElement("input");
                timelineInput.type = "range";
                timelineInput.min = "0";
                timelineInput.max = "0";
                timelineInput.step = "1";
                timelineInput.value = "0";
                timelineInput.disabled = true;
                timelineInput.style.width = "100%";
                timelineInput.addEventListener("input", () => {
                    if (!state.video || !state.fps) return;
                    if (!state.video.paused) state.video.pause();
                    const frame = Number(timelineInput.value);
                    const mediaTime = state.startTime + frame / state.fps;
                    state.video.currentTime = Math.min(state.video.duration || Infinity, mediaTime);
                    updateVideoFrame(mediaTime);
                });

                playerHost = document.createElement("div");
                Object.assign(playerHost.style, {
                    minHeight: "120px",
                    background: "#080808",
                    borderRadius: "4px",
                    overflow: "hidden",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                });
                resultCanvas = document.createElement("canvas");
                resultCanvas.style.display = "block";
                resultCanvas.style.maxWidth = "100%";
                resultCanvas.style.maxHeight = "100%";
                resultCanvas.style.width = "auto";
                resultCanvas.style.height = "auto";
                resultCanvas.style.cursor = "pointer";
                resultCanvas.title = "Play / pause";
                resultCanvas.addEventListener("click", toggleVideoPlayback);
                maskCanvas = document.createElement("canvas");
                maskOverlayCanvas = document.createElement("canvas");
                playerHost.appendChild(resultCanvas);
                playerResizeObserver = new ResizeObserver(() => renderResultPreview());
                playerResizeObserver.observe(playerHost);

                const controls = document.createElement("div");
                Object.assign(controls.style, {
                    display: "grid",
                    gridTemplateColumns: "auto 1fr auto",
                    alignItems: "center",
                    gap: "8px",
                    color: "#aaa",
                    font: "11px sans-serif",
                });
                playButton = document.createElement("button");
                playButton.type = "button";
                playButton.textContent = "▶";
                Object.assign(playButton.style, {
                    minWidth: "30px",
                    height: "24px",
                    border: "1px solid #555",
                    borderRadius: "4px",
                    background: "#222",
                    color: "#ddd",
                    cursor: "pointer",
                });
                playButton.addEventListener("click", toggleVideoPlayback);
                previewStatus = document.createElement("span");
                previewStatus.textContent = "Result preview";
                const volumeInput = document.createElement("input");
                volumeInput.type = "range";
                volumeInput.min = "0";
                volumeInput.max = "1";
                volumeInput.step = "0.05";
                volumeInput.value = "1";
                volumeInput.title = "Volume";
                volumeInput.style.width = "64px";
                volumeInput.addEventListener("input", () => {
                    if (state.video) state.video.volume = Number(volumeInput.value);
                });
                controls.append(playButton, previewStatus, volumeInput);
                panel.append(timelineHeader, timelineInput, playerHost, controls);

                node.addDOMWidget("jh_video_player", "jh_video_player", panel, {
                    serialize: false,
                    hideOnZoom: false,
                    getMinHeight: () => {
                        const innerWidth = Math.max(180, node.size[0] - MARGIN * 2);
                        const crop = previewCropRect();
                        const ratio = mediaWidth() > 0 ? mediaHeight() * crop.h / (mediaWidth() * crop.w) : 9 / 16;
                        return Math.max(220, Math.min(470, innerWidth * ratio + 78));
                    },
                });
            }

            let dynamicWidgetId = 0;

            function addDynamicWidget(type, name, value, callback, options = {}) {
                const widget = node.addWidget(type, `jh_mask_dynamic_${dynamicWidgetId++}`, value, callback, options);
                widget.label = name;
                widget.serialize = false;
                widget.options = { ...(widget.options || {}), serialize: false };
                widget._jhMaskDynamic = true;
                return widget;
            }

            function bindSlotValue(widget, slot, field, normalize) {
                widget._jhMaskSlot = slot;
                widget._jhMaskField = field;
                widget._jhMaskNormalize = normalize;
                return widget;
            }

            function syncDynamicValues() {
                for (const widget of node.widgets) {
                    if (!widget._jhMaskSlot || !widget._jhMaskField) continue;
                    widget._jhMaskSlot[widget._jhMaskField] = widget._jhMaskNormalize(widget.value);
                }
            }

            configWidget.serializeValue = () => {
                syncDynamicValues();
                syncConfig();
                return configWidget.value;
            };

            function rebuildSlotWidgets() {
                for (let index = node.widgets.length - 1; index >= 0; index--) {
                    if (!node.widgets[index]._jhMaskDynamic) continue;
                    node.widgets[index].onRemove?.();
                    node.widgets.splice(index, 1);
                }
                node._widgetSlotsDirty = true;
                state.slots.forEach((slot, index) => {
                    addDynamicWidget("combo", `slot ${index + 1}`, slot.mode.toUpperCase(), (value) => {
                        slot.mode = String(value).toLowerCase();
                        if (slot.mode === "sam2" || slot.mode === "sam3") slot.model = modelForMode(slot.mode, slot.model);
                        state.active = index;
                        syncConfig();
                        rebuildSlotWidgets();
                    }, { values: isVideo ? ["CROP", "MASKING", "SAM3"] : ["CROP", "MASKING", "BACKGROUND", ...(sam2ModelValues.length ? ["SAM2"] : []), "SAM3"] });

                    if (slot.mode !== "crop" && !(slot.mode === "masking" && slot.masking_prompt_mode === "brush")) {
                        addDynamicWidget("combo", "combine", slot.operation.toUpperCase(), (value) => {
                            slot.operation = String(value).toLowerCase();
                            state.active = index;
                            syncConfig();
                        }, { values: ["ADD", "SUBTRACT", "INTERSECT", "XOR"] });
                    }
                    if (slot.mode === "masking") {
                        bindSlotValue(addDynamicWidget("combo", "prompt_mode", String(slot.masking_prompt_mode || "box").toUpperCase(), (value) => {
                            slot.masking_prompt_mode = String(value).toLowerCase();
                            state.active = index;
                            syncConfig();
                            rebuildSlotWidgets();
                        }, { values: ["BOX", "BRUSH"] }), slot, "masking_prompt_mode", (value) => String(value).toLowerCase());
                        if (slot.masking_prompt_mode === "brush") {
                            bindSlotValue(addDynamicWidget("combo", "brush", String(slot.masking_brush_mode || "include").toUpperCase(), (value) => {
                                slot.masking_brush_mode = String(value).toLowerCase();
                                state.active = index;
                                syncConfig();
                            }, { values: ["INCLUDE", "EXCLUDE"] }), slot, "masking_brush_mode", (value) => String(value).toLowerCase());
                            bindSlotValue(addDynamicWidget("number", "brush_size", slot.masking_brush_size, (value) => { slot.masking_brush_size = Math.round(value); syncConfig(); }, { min: 2, max: 256, step: 10, step2: 1, precision: 0 }), slot, "masking_brush_size", (value) => Math.round(value));
                            addDynamicWidget("button", "undo brush stroke", null, () => {
                                slot.masking_brush_strokes?.pop();
                                state.active = index;
                                syncConfig();
                                redrawEditor();
                            });
                            addDynamicWidget("button", "clear brush strokes", null, () => {
                                slot.masking_brush_strokes = [];
                                state.active = index;
                                syncConfig();
                                redrawEditor();
                            });
                        }
                    }
                    if (slot.mode === "sam3") {
                        bindSlotValue(addDynamicWidget("text", "object", slot.prompt, (value) => { slot.prompt = String(value); state.active = index; syncConfig(); }), slot, "prompt", String);
                        bindSlotValue(addDynamicWidget("number", "threshold", slot.threshold, (value) => { slot.threshold = Number(value); syncConfig(); }, { min: 0, max: 1, step: 0.1, step2: 0.01, precision: 2 }), slot, "threshold", Number);
                        bindSlotValue(addDynamicWidget("number", "refine_iterations", slot.refine_iterations, (value) => { slot.refine_iterations = Math.round(value); syncConfig(); }, { min: 0, max: 5, step: 10, step2: 1, precision: 0 }), slot, "refine_iterations", (value) => Math.round(value));
                        bindSlotValue(addDynamicWidget("number", "grow_mask", slot.grow_mask, (value) => { slot.grow_mask = Math.round(value); syncConfig(); }, { min: -512, max: 512, step: 10, step2: 1, precision: 0 }), slot, "grow_mask", (value) => Math.round(value));
                        bindSlotValue(addDynamicWidget("number", "feather_mask", slot.feather_mask, (value) => { slot.feather_mask = Math.round(value); syncConfig(); }, { min: 0, max: 256, step: 10, step2: 1, precision: 0 }), slot, "feather_mask", (value) => Math.round(value));
                        if (!isVideo) {
                            bindSlotValue(addDynamicWidget("toggle", "individual_masks", slot.individual_masks, (value) => { slot.individual_masks = Boolean(value); syncConfig(); }), slot, "individual_masks", Boolean);
                        }
                        bindSlotValue(addDynamicWidget("combo", "ckpt_name", modelForMode("sam3", slot.model || modelWidget.value), (value) => { slot.model = String(value); syncConfig(); }, { values: sam3ModelValues }), slot, "model", String);
                    }
                    if (slot.mode === "sam2") {
                        bindSlotValue(addDynamicWidget("combo", "prompt_mode", String(slot.sam2_prompt_mode || "brush").toUpperCase(), (value) => {
                            slot.sam2_prompt_mode = String(value).toLowerCase();
                            state.active = index;
                            syncConfig();
                            rebuildSlotWidgets();
                        }, { values: ["BRUSH", "BOX"] }), slot, "sam2_prompt_mode", (value) => String(value).toLowerCase());
                        if (slot.sam2_prompt_mode !== "box") {
                            bindSlotValue(addDynamicWidget("combo", "brush", String(slot.sam2_brush_mode || "include").toUpperCase(), (value) => {
                                slot.sam2_brush_mode = String(value).toLowerCase();
                                state.active = index;
                                syncConfig();
                            }, { values: ["INCLUDE", "EXCLUDE"] }), slot, "sam2_brush_mode", (value) => String(value).toLowerCase());
                            bindSlotValue(addDynamicWidget("number", "brush_size", slot.sam2_brush_size, (value) => { slot.sam2_brush_size = Math.round(value); syncConfig(); }, { min: 2, max: 256, step: 10, step2: 1, precision: 0 }), slot, "sam2_brush_size", (value) => Math.round(value));
                            addDynamicWidget("button", "undo brush stroke", null, () => {
                                const stroke = slot.sam2_brush_history?.pop();
                                if (!stroke || !Array.isArray(slot[stroke.field])) return;
                                slot[stroke.field].splice(-stroke.count, stroke.count);
                                state.active = index;
                                syncConfig();
                                redrawEditor();
                            });
                            addDynamicWidget("button", "clear brush prompts", null, () => {
                                slot.sam2_positive_points = [];
                                slot.sam2_negative_points = [];
                                slot.sam2_brush_history = [];
                                state.active = index;
                                syncConfig();
                                redrawEditor();
                            });
                        }
                        bindSlotValue(addDynamicWidget("number", "grow_mask", slot.grow_mask, (value) => { slot.grow_mask = Math.round(value); syncConfig(); }, { min: -512, max: 512, step: 10, step2: 1, precision: 0 }), slot, "grow_mask", (value) => Math.round(value));
                        bindSlotValue(addDynamicWidget("number", "feather_mask", slot.feather_mask, (value) => { slot.feather_mask = Math.round(value); syncConfig(); }, { min: 0, max: 256, step: 10, step2: 1, precision: 0 }), slot, "feather_mask", (value) => Math.round(value));
                        bindSlotValue(addDynamicWidget("combo", "ckpt_name", modelForMode("sam2", slot.model), (value) => { slot.model = String(value); syncConfig(); }, { values: sam2ModelValues }), slot, "model", String);
                    }
                    if (slot.mode === "background") {
                        addDynamicWidget("text", "model", "RMBG-2.0", null, { disabled: true });
                        bindSlotValue(addDynamicWidget("number", "grow_foreground", slot.grow_mask, (value) => { slot.grow_mask = Math.round(value); syncConfig(); }, { min: -512, max: 512, step: 10, step2: 1, precision: 0 }), slot, "grow_mask", (value) => Math.round(value));
                        bindSlotValue(addDynamicWidget("number", "feather_foreground", slot.feather_mask, (value) => { slot.feather_mask = Math.round(value); syncConfig(); }, { min: 0, max: 256, step: 10, step2: 1, precision: 0 }), slot, "feather_mask", (value) => Math.round(value));
                    }
                    if (state.slots.length > 1) {
                        addDynamicWidget("button", `- remove slot ${index + 1}`, null, () => {
                            state.slots.splice(index, 1);
                            state.active = Math.max(0, Math.min(state.active, state.slots.length - 1));
                            syncConfig();
                            rebuildSlotWidgets();
                        });
                    }
                });
                addDynamicWidget("button", "+ add slot", null, () => {
                    state.slots.push({ ...defaultSlot(modelWidget.value), mode: "masking" });
                    state.active = state.slots.length - 1;
                    syncConfig();
                    rebuildSlotWidgets();
                });
                addDynamicWidget("button", "reset slots", null, () => {
                    state.slots = [defaultSlot(modelWidget.value)];
                    state.active = 0;
                    syncConfig();
                    rebuildSlotWidgets();
                });
                node.setSize([node.size[0], Math.max(node.size[1], node.computeSize()[1])]);
                node.setDirtyCanvas(true, true);
            }

            let loadSequence = 0;
            let lastLiveDraw = 0;

            async function loadVideoInfo(value, sequence) {
                try {
                    const response = await api.fetchApi(`/jh/video-info?video=${encodeURIComponent(value)}`);
                    if (!response.ok) return;
                    const info = await response.json();
                    if (sequence !== loadSequence) return;
                    state.sourceFps = Number(info.fps) || 0;
                    state.sourceFrameCount = Number(info.frame_count) || 0;
                    state.sourceDuration = Number(info.duration) || 0;
                    updateVideoRange(true);
                } catch (_) {
                    // The player remains usable even if exact frame metadata is unavailable.
                }
            }

            function startLiveSync(video, sequence) {
                if (!video.requestVideoFrameCallback) return;
                const drawFrame = (now, metadata) => {
                    if (sequence !== loadSequence || state.video !== video) return;
                    if (now - lastLiveDraw >= 66) {
                        lastLiveDraw = now;
                        updateVideoFrame(metadata.mediaTime);
                    }
                    if (!video.paused && !video.ended) video.requestVideoFrameCallback(drawFrame);
                };
                video.requestVideoFrameCallback(drawFrame);
            }

            function loadImage() {
                const sequence = ++loadSequence;
                const info = parseImageValue(imageWidget.value);
                if (!info) return;
                const src = api.apiURL(`/view?filename=${encodeURIComponent(info.filename)}&type=${info.type}&subfolder=${encodeURIComponent(info.subfolder)}&rand=${Math.random()}`);
                if (isVideo) {
                    if (state.video) {
                        state.video.pause();
                        state.video.removeAttribute("src");
                        state.video.load();
                    }
                    state.img = null;
                    state.video = null;
                    state.fps = 0;
                    state.frameCount = 0;
                    state.availableFrameCount = 0;
                    state.sourceFps = 0;
                    state.sourceFrameCount = 0;
                    state.sourceDuration = 0;
                    state.startTime = 0;
                    state.endTime = 0;
                    forceRateWidget.options.reset = 0;
                    frameLoadCapWidget.options.reset = 0;
                    skipFirstFramesWidget.options.reset = 0;
                    state.box = null;
                    if (resultCanvas) {
                        const context = resultCanvas.getContext("2d");
                        context.clearRect(0, 0, resultCanvas.width, resultCanvas.height);
                    }
                    if (playButton) playButton.textContent = "▶";
                    if (frameLabel) frameLabel.textContent = "Frame 1 / ?";
                    if (timelineInput) {
                        timelineInput.value = "0";
                        timelineInput.max = "0";
                        timelineInput.disabled = true;
                    }
                    const video = document.createElement("video");
                    video.preload = "auto";
                    video.playsInline = true;
                    state.video = video;
                    loadVideoInfo(imageWidget.value, sequence);
                    video.onloadeddata = () => {
                        if (sequence !== loadSequence) return;
                        state.img = video;
                        if (video.currentTime === 0 && video.duration > 0.002) {
                            video.currentTime = Math.min(video.duration, state.startTime + 0.001);
                        } else {
                            updateVideoFrame();
                        }
                        node.setSize([node.size[0], Math.max(node.size[1], node.computeSize()[1])]);
                    };
                    video.addEventListener("seeked", () => updateVideoFrame());
                    video.addEventListener("pause", () => {
                        if (playButton) playButton.textContent = "▶";
                        updateVideoFrame();
                    });
                    video.addEventListener("timeupdate", () => {
                        if (!video.requestVideoFrameCallback) updateVideoFrame();
                    });
                    video.addEventListener("play", () => {
                        if (playButton) playButton.textContent = "Ⅱ";
                        startLiveSync(video, sequence);
                    });
                    video.addEventListener("ended", () => {
                        if (playButton) playButton.textContent = "▶";
                        updateVideoFrame();
                    });
                    video.onerror = () => {
                        if (sequence !== loadSequence) return;
                        state.img = null;
                        redrawEditor();
                    };
                    video.src = src;
                    video.load();
                    return;
                }
                const image = new Image();
                image.onload = () => {
                    if (sequence !== loadSequence) return;
                    state.img = image;
                    editorWidget.triggerDraw?.();
                    node.setDirtyCanvas(true, true);
                };
                image.onerror = () => { if (sequence === loadSequence) state.img = null; };
                image.src = src;
            }

            const imageCallback = imageWidget.callback;
            imageWidget.callback = function () {
                const callbackResult = imageCallback?.apply(this, arguments);
                for (const slot of state.slots) slot.rect = null;
                syncConfig();
                loadImage();
                return callbackResult;
            };

            const megapixelsCallback = megapixelsWidget.callback;
            megapixelsWidget.callback = function (value) {
                const maxMegapixels = Number(value);
                megapixelsWidget.value = Number.isFinite(maxMegapixels) ? maxMegapixels : 0;
                node.properties = node.properties || {};
                node.properties.jh_load_image_mask_max_megapixels = megapixelsWidget.value;
                node.setDirtyCanvas(true, true);
                return megapixelsCallback?.apply(this, [megapixelsWidget.value]);
            };

            const selectionCallback = selectionWidget.callback;
            selectionWidget.callback = function (value) {
                const callbackResult = selectionCallback?.apply(this, arguments);
                selectionWidget.value = value;
                syncConfig();
                return callbackResult;
            };

            if (isVideo) {
                for (const [widget, normalize] of [
                    [forceRateWidget, (value) => Math.max(0, Number(value) || 0)],
                    [frameLoadCapWidget, (value) => Math.max(0, Math.round(Number(value) || 0))],
                    [skipFirstFramesWidget, (value) => Math.max(0, Math.round(Number(value) || 0))],
                ]) {
                    const callback = widget.callback;
                    widget.callback = function (value) {
                        const callbackResult = callback?.apply(this, arguments);
                        widget.value = normalize(value);
                        updateVideoRange(true);
                        return callbackResult;
                    };
                }
            }

            const onConfigure = node.onConfigure;
            node.onConfigure = function (info) {
                const configuredValues = Array.isArray(info?.widgets_values) ? [...info.widgets_values] : null;
                const configuredImageProperty = info?.properties?.jh_load_image_mask_image;
                const configuredProperty = info?.properties?.jh_load_image_mask_slots;
                const configuredMaxProperty = Number(info?.properties?.jh_load_image_mask_max_megapixels);
                const configuredSelectionProperty = info?.properties?.jh_load_image_mask_selection_mode;
                const configuredModelProperty = info?.properties?.jh_load_image_mask_model;
                const configuredHotkeys = info?.properties?.jh_load_image_mask_hotkeys;
                const configureResult = onConfigure?.apply(this, arguments);
                const configuredImageValue = typeof configuredValues?.[0] === "string"
                    ? configuredValues[0]
                    : "";
                const restoredImage = typeof configuredImageProperty === "string" && configuredImageProperty
                    ? configuredImageProperty
                    : configuredImageValue;
                if (restoredImage) imageWidget.value = restoredImage;
                let restored = typeof configuredProperty === "string" ? configuredProperty : null;
                if (!restored && Array.isArray(configuredValues)) {
                    restored = configuredValues.find((value) => {
                        if (typeof value !== "string" || !value.startsWith("{")) return false;
                        try {
                            return Array.isArray(JSON.parse(value).slots);
                        } catch (_) {
                            return false;
                        }
                    }) || null;
                }
                if (restored) {
                    configWidget.value = restored;
                    node.properties = node.properties || {};
                    node.properties.jh_load_image_mask_slots = restored;
                }
                const configuredMaxValue = Array.isArray(configuredValues)
                    ? configuredValues.find((value) => typeof value === "number" && Number.isFinite(value))
                    : undefined;
                const restoredMax = Number.isFinite(configuredMaxProperty)
                    ? configuredMaxProperty
                    : (configuredMaxValue ?? 0);
                megapixelsWidget.value = restoredMax;
                node.properties = node.properties || {};
                node.properties.jh_load_image_mask_max_megapixels = restoredMax;
                const selectionValues = ["REMOVE SELECTED", "KEEP SELECTED"];
                const configuredSelectionValue = Array.isArray(configuredValues)
                    ? configuredValues.find((value) => selectionValues.includes(value))
                    : undefined;
                selectionWidget.value = selectionValues.includes(configuredSelectionProperty)
                    ? configuredSelectionProperty
                    : (configuredSelectionValue || "REMOVE SELECTED");
                node.properties.jh_load_image_mask_selection_mode = selectionWidget.value;
                const configuredModelValue = Array.isArray(configuredValues)
                    ? configuredValues.find((value) => typeof value === "string" && modelValues.includes(value))
                    : undefined;
                const restoredModel = typeof configuredModelProperty === "string" && modelValues.includes(configuredModelProperty)
                    ? configuredModelProperty
                    : configuredModelValue;
                if (restoredModel) modelWidget.value = restoredModel;
                if (clipboardActions && configuredHotkeys && typeof configuredHotkeys === "object") {
                    clipboardActions.hotkeys.run = String(configuredHotkeys.run || "");
                    clipboardActions.hotkeys.paste = String(configuredHotkeys.paste || "");
                }
                readConfig();
                syncConfig();
                requestAnimationFrame(() => {
                    rebuildSlotWidgets();
                    loadImage();
                });
                return configureResult;
            };

            const onSerialize = node.onSerialize;
            node.onSerialize = function (info) {
                syncDynamicValues();
                syncConfig();
                const serializeResult = onSerialize?.apply(this, arguments);
                info.properties = info.properties || {};
                info.properties.jh_load_image_mask_image = String(imageWidget.value || "");
                info.properties.jh_load_image_mask_slots = configWidget.value;
                info.properties.jh_load_image_mask_max_megapixels = Number.isFinite(Number(megapixelsWidget.value))
                    ? Number(megapixelsWidget.value)
                    : 0;
                info.properties.jh_load_image_mask_selection_mode = selectionWidget.value;
                info.properties.jh_load_image_mask_model = String(modelWidget.value || "");
                if (clipboardActions) info.properties.jh_load_image_mask_hotkeys = { ...clipboardActions.hotkeys };
                info.widgets_values = Array.isArray(info.widgets_values) ? info.widgets_values : [];
                for (const widget of [imageWidget, configWidget, megapixelsWidget, selectionWidget, modelWidget]) {
                    const index = node.widgets.findIndex((candidate) => candidate.name === widget.name);
                    if (index >= 0) info.widgets_values[index] = widget.value;
                }
                return serializeResult;
            };

            rebuildSlotWidgets();
            loadImage();
            const onRemoved = node.onRemoved;
            node.onRemoved = function () {
                if (recordingHotkey?.node === node) {
                    recordingHotkey = null;
                    globalThis.jhExternalHotkeyRecording = false;
                }
                playerResizeObserver?.disconnect();
                if (state.video) {
                    state.video.pause();
                    state.video.removeAttribute("src");
                    state.video.load();
                }
                return onRemoved?.apply(this, arguments);
            };
            return result;
        };
    },
});
