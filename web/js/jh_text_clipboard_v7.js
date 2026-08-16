import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";
import { ComfyWidgets } from "../../../scripts/widgets.js";

function setWidgetValue(node, widget, value) {
	if (!widget) {
		return;
	}

	widget.value = value;
	widget.callback?.(value);

	if (widget.inputEl) {
		widget.inputEl.value = value;
		widget.inputEl.dispatchEvent(new Event("input", { bubbles: true }));
		widget.inputEl.dispatchEvent(new Event("change", { bubbles: true }));
	}

	node.setDirtyCanvas?.(true, true);
	app.graph?.setDirtyCanvas?.(true, true);
}

function toast(severity, summary, detail) {
	app.extensionManager?.toast?.add?.({
		severity,
		summary,
		detail,
		life: 3000,
	});
}

function getWidget(node, name) {
	return node.widgets?.find((widget) => widget.name === name);
}

function hideWidget(widget) {
	if (!widget) {
		return;
	}
	widget.hidden = true;
	widget.computeSize = () => [0, -4];
}

function dataUrlToBlob(dataUrl) {
	const parts = dataUrl.split(",");
	const header = parts[0] ?? "";
	const mime = header.match(/data:(.*?);base64/)?.[1] || "image/png";
	const binary = atob(parts[1] ?? "");
	const bytes = new Uint8Array(binary.length);
	for (let i = 0; i < binary.length; i++) {
		bytes[i] = binary.charCodeAt(i);
	}
	return new Blob([bytes], { type: mime });
}

function blobToDataUrl(blob) {
	return new Promise((resolve, reject) => {
		const reader = new FileReader();
		reader.onload = () => resolve(reader.result);
		reader.onerror = reject;
		reader.readAsDataURL(blob);
	});
}

async function readClipboardImageAsDataUrl() {
	const items = await navigator.clipboard.read();
	for (const item of items) {
		const imageType = item.types.find((type) => type.startsWith("image/"));
		if (imageType) {
			const blob = await item.getType(imageType);
			return blob;
		}
	}
	return null;
}

async function normalizeImageBlobToPng(blob) {
	if (blob.type !== "image/png") {
		const bitmap = await createImageBitmap(blob);
		const canvas = document.createElement("canvas");
		canvas.width = bitmap.width;
		canvas.height = bitmap.height;
		const ctx = canvas.getContext("2d");
		ctx.drawImage(bitmap, 0, 0);
		blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
	}
	return blob;
}

async function writeImageBlobToClipboard(blob) {
	blob = await normalizeImageBlobToPng(blob);
	await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
}

async function writeImageValueToClipboard(value) {
	if (!value) {
		return;
	}

	if (value.startsWith("data:image")) {
		await writeImageBlobToClipboard(dataUrlToBlob(value));
		return;
	}

	const response = await fetch(imageValueToViewUrl(value), { cache: "no-store" });
	if (!response.ok) {
		throw new Error(`Image fetch failed: ${response.status}`);
	}
	await writeImageBlobToClipboard(await response.blob());
}

function imageValueToParts(value) {
	const normalized = (value || "").replaceAll("\\", "/");
	const parts = normalized.split("/");
	const filename = parts.pop() || "";
	const subfolder = parts.join("/");
	return { filename, subfolder };
}

function imageValueToViewUrl(value) {
	if (!value) {
		return "";
	}
	if (value.startsWith("data:image")) {
		return value;
	}
	const { filename, subfolder } = imageValueToParts(value);
	const params = new URLSearchParams({
		filename,
		type: "input",
		subfolder,
		rand: Math.random().toString(),
	});
	return api.apiURL(`/view?${params.toString()}`);
}

async function uploadClipboardImageBlob(blob) {
	const pngBlob = await normalizeImageBlobToPng(blob);
	const now = new Date();
	const stamp = now.toISOString().replace(/[-:.TZ]/g, "").slice(0, 14);
	const random = Math.random().toString(36).slice(2, 8);
	const file = new File([pngBlob], `jh_clipboard_${stamp}_${random}.png`, { type: "image/png" });
	const formData = new FormData();
	formData.append("image", file);
	formData.append("type", "input");
	formData.append("subfolder", "jh_clipboard");
	formData.append("overwrite", "false");

	const response = await api.fetchApi("/upload/image", { method: "POST", body: formData });
	if (!response.ok) {
		throw new Error(`Image upload failed: ${response.status}`);
	}
	const data = await response.json();
	return [data.subfolder, data.name].filter(Boolean).join("/");
}

function getImageFileFromDropEvent(event) {
	const files = Array.from(event?.dataTransfer?.files || []);
	const imageExtensions = /\.(png|jpe?g|webp|bmp|gif|tiff?)$/i;
	return files.find((file) => file?.type?.startsWith("image/") || imageExtensions.test(file?.name || "")) || null;
}

function hasImageFileDrop(event) {
	const imageExtensions = /\.(png|jpe?g|webp|bmp|gif|tiff?)$/i;
	const items = Array.from(event?.dataTransfer?.items || []);
	if (items.some((item) => item.kind === "file" && (!item.type || item.type.startsWith("image/")))) {
		return true;
	}

	const files = Array.from(event?.dataTransfer?.files || []);
	return files.some((file) => file?.type?.startsWith("image/") || imageExtensions.test(file?.name || ""));
}

function getPromptMediaFileFromDropEvent(event) {
	const files = Array.from(event?.dataTransfer?.files || []);
	const mediaExtensions = /\.(png|jpe?g|webp|gif|bmp|tiff?|mp4|mov|m4v|mkv|webm|avi)$/i;
	return files.find((file) => file && (file.type?.startsWith("image/") || file.type?.startsWith("video/") || mediaExtensions.test(file.name || ""))) || null;
}

function hasPromptMediaFileDrop(event) {
	const mediaExtensions = /\.(png|jpe?g|webp|gif|bmp|tiff?|mp4|mov|m4v|mkv|webm|avi)$/i;
	const items = Array.from(event?.dataTransfer?.items || []);
	if (items.some((item) => item.kind === "file" && (!item.type || item.type.startsWith("image/") || item.type.startsWith("video/")))) {
		return true;
	}
	return Array.from(event?.dataTransfer?.files || []).some((file) => file && (file.type?.startsWith("image/") || file.type?.startsWith("video/") || mediaExtensions.test(file.name || "")));
}

async function readPromptFromMediaFile(file) {
	const formData = new FormData();
	formData.append("media", file, file.name);
	const response = await api.fetchApi("/jh/clipboard-text/media-prompt", { method: "POST", body: formData });
	const data = await response.json().catch(() => ({}));
	if (!response.ok || typeof data.text !== "string") {
		throw new Error(data.error || `Metadata read failed: ${response.status}`);
	}
	return data.text;
}

async function setClipboardNodeImageFromBlob(node, imageWidget, previousWidget, blob) {
	const imagePath = await uploadClipboardImageBlob(blob);
	setWidgetValue(node, previousWidget, imageWidget.value || "");
	setWidgetValue(node, imageWidget, imagePath);
	refreshImagePreview(node);
}

async function migrateDataUrlWidgetToFile(node, widget) {
	if (!widget?.value?.startsWith?.("data:image")) {
		return;
	}

	try {
		const path = await uploadClipboardImageBlob(dataUrlToBlob(widget.value));
		setWidgetValue(node, widget, path);
		refreshImagePreview(node);
		toast("info", "JH Image Clipboard", "Embedded image was moved to input/jh_clipboard.");
	} catch (error) {
		console.error("[JH Image Clipboard] Migration failed:", error);
	}
}

function refreshImagePreview(node) {
	const imageData = getWidget(node, "image_data")?.value || "";
	if (!imageData) {
		node.jhPreviewImage = null;
		node.setDirtyCanvas?.(true, true);
		return;
	}

	const img = new Image();
	img.onload = () => {
		node.jhPreviewImage = img;
		node.setDirtyCanvas?.(true, true);
		app.graph?.setDirtyCanvas?.(true, true);
	};
	img.onerror = () => {
		node.jhPreviewImage = null;
		node.setDirtyCanvas?.(true, true);
		app.graph?.setDirtyCanvas?.(true, true);
	};
	img.src = imageValueToViewUrl(imageData);
}

let recordingHotkey = null;

function hotkeyFromEvent(event) {
	const modifierKeys = new Set(["Control", "Alt", "Shift", "Meta"]);
	if (modifierKeys.has(event.key)) {
		return "";
	}

	const keyNames = {
		" ": "Space",
		Escape: "Esc",
		ArrowUp: "Up",
		ArrowDown: "Down",
		ArrowLeft: "Left",
		ArrowRight: "Right",
	};
	let key = keyNames[event.key] || event.key;
	if (key.length === 1) {
		key = key.toUpperCase();
	}

	return [
		event.ctrlKey ? "Ctrl" : "",
		event.altKey ? "Alt" : "",
		event.shiftKey ? "Shift" : "",
		event.metaKey ? "Meta" : "",
		key,
	].filter(Boolean).join("+");
}

function startHotkeyRecording(node, widget) {
	if (recordingHotkey) {
		recordingHotkey.widget.recording = false;
		recordingHotkey.node.setDirtyCanvas?.(true, false);
	}
	recordingHotkey = { node, widget };
	widget.recording = true;
	if (widget.displayPrefix) {
		widget.label = `${widget.displayPrefix}: PRESS SHORTCUT...`;
	}
	node.setDirtyCanvas?.(true, false);
	toast("info", "Shortcut", "Press a shortcut. Delete clears it; Esc cancels.");
}

function finishHotkeyRecording(value) {
	if (!recordingHotkey) {
		return;
	}
	const { node, widget } = recordingHotkey;
	if (value !== null) {
		widget.value = value;
		node.graph?.change?.();
		app.graph?.setDirtyCanvas?.(true, true);
	}
	widget.recording = false;
	if (widget.displayPrefix) {
		widget.label = `${widget.displayPrefix}: ${widget.value || "NOT SET"}`;
	}
	recordingHotkey = null;
	node.setDirtyCanvas?.(true, false);
}

function isTextInput(target) {
	return target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target?.isContentEditable;
}

function isNodeSelected(node) {
	const selected = app.canvas?.selected_nodes;
	if (selected instanceof Map) {
		return selected.has(node.id) || Array.from(selected.values()).includes(node);
	}
	return Boolean(selected?.[node.id] || node.is_selected);
}

async function handleClipboardHotkey(event) {
	if (recordingHotkey) {
		event.preventDefault();
		event.stopPropagation();
		if (event.key === "Escape") {
			finishHotkeyRecording(null);
			return;
		}
		if (event.key === "Delete" || event.key === "Backspace") {
			finishHotkeyRecording("");
			return;
		}
		const hotkey = hotkeyFromEvent(event);
		if (hotkey) {
			finishHotkeyRecording(hotkey);
		}
		return;
	}
	if (globalThis.jhExternalHotkeyRecording) {
		return;
	}

	if (event.repeat || isTextInput(event.target)) {
		return;
	}
	const hotkey = hotkeyFromEvent(event);
	if (!hotkey) {
		return;
	}
	const matches = (app.graph?._nodes || []).flatMap((node) => {
		const bindings = node.jhHotkeyBindings?.() || [];
		const widget = getWidget(node, node.jhHotkeyWidgetName);
		if (widget?.value === hotkey && node.jhHotkeyAction) {
			bindings.push({ value: widget.value, run: node.jhHotkeyAction });
		}
		return bindings
			.filter((binding) => binding.value === hotkey && binding.run)
			.map((binding) => ({ node, run: binding.run }));
	});
	if (!matches.length) {
		return;
	}
	event.preventDefault();
	event.stopPropagation();

	let match = matches[0];
	if (matches.length > 1) {
		const selectedMatches = matches.filter(({ node }) => isNodeSelected(node));
		if (selectedMatches.length !== 1) {
			toast("warn", "Shortcut", "Select one node that uses this shortcut.");
			return;
		}
		match = selectedMatches[0];
	}

	await match.run();
}

window.addEventListener("keydown", handleClipboardHotkey, true);

function makeClipboardImageActions(onPaste, onPasteAndRun, onCopy, onUndo) {
	const actions = [
		{ id: "run", label: "PASTE & RUN", color: "#176b87", hover: "#2187a8", run: onPasteAndRun },
		{ id: "paste", label: "PASTE ONLY", color: "#3b4654", hover: "#526173", run: onPaste },
		{ id: "copy", label: "COPY", color: "#3b4654", hover: "#526173", run: onCopy },
		{ id: "undo", label: "UNDO", color: "#292d33", hover: "#3a4048", run: onUndo },
		{ id: "hotkey", label: (widget) => widget.recording ? "PRESS SHORTCUT..." : `HOTKEY  ·  ${widget.value || "NOT SET"}`, color: "#40334d", hover: "#59436d", run: startHotkeyRecording },
	];

	return {
		type: "jh_clipboard_image_actions",
		name: "jh_clipboard_image_actions",
		value: "Alt+Shift+V",
		options: { serialize: true },
		serialize: true,
		recording: false,
		pressed: null,
		hovered: null,
		bounds: {},
		computeSize(width) {
			return [width || 0, 116];
		},
		draw(ctx, node, width, y) {
			const margin = 15;
			const gap = 8;
			const innerWidth = width - margin * 2;
			const undoWidth = 62;
			const pasteWidth = Math.floor((innerWidth - gap * 2 - undoWidth) * 0.54);
			const copyWidth = innerWidth - gap * 2 - undoWidth - pasteWidth;
			const hotkeyWidth = Math.min(160, Math.floor(innerWidth * 0.55));
			this.bounds.run = [margin, y, innerWidth, 36];
			this.bounds.paste = [margin, y + 52, pasteWidth, 24];
			this.bounds.copy = [margin + pasteWidth + gap, y + 52, copyWidth, 24];
			this.bounds.undo = [margin + pasteWidth + gap + copyWidth + gap, y + 52, undoWidth, 24];
			this.bounds.hotkey = [margin + innerWidth - hotkeyWidth, y + 90, hotkeyWidth, 18];

			ctx.save();
			ctx.textAlign = "center";
			ctx.textBaseline = "middle";
			for (const action of actions) {
				const [x, buttonY, buttonWidth, buttonHeight] = this.bounds[action.id];
				ctx.fillStyle = this.hovered === action.id ? action.hover : action.color;
				if (this.pressed === action.id) {
					ctx.globalAlpha = 0.72;
				}
				ctx.beginPath();
				ctx.roundRect(x, buttonY, buttonWidth, buttonHeight, 6);
				ctx.fill();
				ctx.globalAlpha = 1;
				ctx.fillStyle = action.id === "run" ? "#f4fbff" : "#d9dde2";
				ctx.font = action.id === "run" ? "600 12px sans-serif" : "600 9px sans-serif";
				const label = typeof action.label === "function" ? action.label(this) : action.label;
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
					hit.run(node, this);
				}
				return Boolean(pressed);
			}
			return false;
		},
	};
}

function installClipboardImageNode(node) {
	const imageWidget = getWidget(node, "image_data");
	const previousWidget = getWidget(node, "previous_image_data");
	if (!imageWidget || !previousWidget) {
		return;
	}

	hideWidget(imageWidget);
	hideWidget(previousWidget);
	refreshImagePreview(node);
	migrateDataUrlWidgetToFile(node, imageWidget);
	migrateDataUrlWidgetToFile(node, previousWidget);

	if (!node.widgets?.some((widget) => widget.name === "jh_clipboard_image_actions")) {
		const pasteImage = async () => {
			try {
				const blob = await readClipboardImageAsDataUrl();
				if (!blob) {
					toast("warn", "Clipboard", "No image was found in the clipboard.");
					return false;
				}
				await setClipboardNodeImageFromBlob(node, imageWidget, previousWidget, blob);
				return true;
			} catch (error) {
				console.error("[JH Image Clipboard] Clipboard paste failed:", error);
				toast("warn", "Clipboard", "Clipboard image could not be read.");
				return false;
			}
		};

		const copyImage = async () => {
			try {
				if (!imageWidget.value) {
					toast("warn", "Clipboard", "There is no image to copy.");
					return;
				}
				await writeImageValueToClipboard(imageWidget.value);
				toast("success", "Clipboard", "Image copied.");
			} catch (error) {
				console.error("[JH Image Clipboard] Clipboard copy failed:", error);
				toast("warn", "Clipboard", "Image could not be copied.");
			}
		};

		const undoImage = () => {
			const current = imageWidget.value || "";
			const previous = previousWidget.value || "";
			if (!previous) {
				toast("info", "Undo", "No previous image is stored.");
				return;
			}
			setWidgetValue(node, imageWidget, previous);
			setWidgetValue(node, previousWidget, current);
			refreshImagePreview(node);
		};

		node.jhPasteAndRun = async () => {
			if (node.jhClipboardBusy) {
				return;
			}
			node.jhClipboardBusy = true;
			try {
				if (await pasteImage()) {
					await app.queuePrompt(0, 1);
				}
			} catch (error) {
				console.error("[JH Image Clipboard] Queue failed:", error);
				toast("warn", "JH Image Clipboard", "The image was pasted, but the workflow could not be queued.");
			} finally {
				node.jhClipboardBusy = false;
			}
		};

		node.jhHotkeyWidgetName = "jh_clipboard_image_actions";
		node.jhHotkeyAction = node.jhPasteAndRun;
		node.addCustomWidget(makeClipboardImageActions(pasteImage, node.jhPasteAndRun, copyImage, undoImage));
	}

	const originalOnDragOver = node.onDragOver;
	node.onDragOver = function (event) {
		if (hasImageFileDrop(event)) {
			return true;
		}
		return originalOnDragOver?.apply(this, arguments) ?? false;
	};

	const originalOnDragDrop = node.onDragDrop;
	node.onDragDrop = async function (event) {
		const file = getImageFileFromDropEvent(event);
		if (!file) {
			return originalOnDragDrop?.apply(this, arguments) ?? false;
		}

		event.preventDefault?.();
		event.stopPropagation?.();
		try {
			await setClipboardNodeImageFromBlob(this, imageWidget, previousWidget, file);
			toast("success", "JH Image Clipboard", "Image loaded from dropped file.");
		} catch (error) {
			console.error("[JH Image Clipboard] File drop failed:", error);
			toast("warn", "JH Image Clipboard", "Dropped image could not be loaded.");
		}
		return true;
	};

	node.size = [Math.max(node.size?.[0] ?? 360, 360), Math.max(node.size?.[1] ?? 402, 402)];

	const originalOnDrawForeground = node.onDrawForeground;
	node.onDrawForeground = function (ctx) {
		originalOnDrawForeground?.apply(this, arguments);
		if (!this.jhPreviewImage) {
			ctx.save();
			ctx.fillStyle = "#777";
			ctx.textAlign = "center";
			ctx.font = "12px sans-serif";
			ctx.fillText("No clipboard image", this.size[0] / 2, Math.max(105, this.size[1] / 2));
			ctx.restore();
			return;
		}

		const pad = 12;
		const top = 129;
		const width = Math.max(1, this.size[0] - pad * 2);
		const height = Math.max(1, this.size[1] - top - pad);
		const scale = Math.min(width / this.jhPreviewImage.width, height / this.jhPreviewImage.height);
		const drawWidth = this.jhPreviewImage.width * scale;
		const drawHeight = this.jhPreviewImage.height * scale;
		const x = pad + (width - drawWidth) / 2;
		const y = top + (height - drawHeight) / 2;

		ctx.save();
		ctx.fillStyle = "#111";
		ctx.fillRect(pad, top, width, height);
		ctx.drawImage(this.jhPreviewImage, x, y, drawWidth, drawHeight);
		ctx.restore();
	};

	const originalOnConfigure = node.onConfigure;
	node.onConfigure = function () {
		originalOnConfigure?.apply(this, arguments);
		requestAnimationFrame(() => {
			hideWidget(getWidget(this, "image_data"));
			hideWidget(getWidget(this, "previous_image_data"));
			migrateDataUrlWidgetToFile(this, getWidget(this, "image_data"));
			migrateDataUrlWidgetToFile(this, getWidget(this, "previous_image_data"));
			refreshImagePreview(this);
		});
	};
}

function getPreviewImage(node) {
	if (!node.imgs?.length) {
		return null;
	}
	const index = Number.isInteger(node.imageIndex) ? node.imageIndex : Number.isInteger(node.overIndex) ? node.overIndex : 0;
	return node.imgs[index] || node.imgs[0];
}

function makeImagePreviewActions(onCopy, onOpen) {
	const actions = [
		{ id: "open", label: "OPEN IMAGE", color: "#176b87", hover: "#2187a8", run: onOpen },
		{ id: "copy", label: "COPY TO CLIPBOARD", color: "#3b4654", hover: "#526173", run: onCopy },
		{ id: "hotkey", label: (widget) => widget.recording ? "PRESS SHORTCUT..." : `HOTKEY  ·  ${widget.value || "NOT SET"}`, color: "#40334d", hover: "#59436d", run: startHotkeyRecording },
	];

	return {
		type: "jh_image_preview_actions",
		name: "jh_image_preview_actions",
		value: "Alt+Shift+O",
		options: { serialize: true },
		serialize: true,
		recording: false,
		pressed: null,
		hovered: null,
		bounds: {},
		computeSize(width) {
			return [width || 0, 110];
		},
		draw(ctx, node, width, y) {
			const margin = 15;
			const innerWidth = width - margin * 2;
			const copyWidth = Math.floor(innerWidth * 0.58);
			const hotkeyWidth = Math.min(160, Math.floor(innerWidth * 0.45));
			this.bounds.open = [margin, y, innerWidth, 36];
			this.bounds.copy = [margin, y + 52, copyWidth, 24];
			this.bounds.hotkey = [margin + innerWidth - hotkeyWidth, y + 84, hotkeyWidth, 18];

			ctx.save();
			ctx.textAlign = "center";
			ctx.textBaseline = "middle";
			for (const action of actions) {
				const [x, buttonY, buttonWidth, buttonHeight] = this.bounds[action.id];
				ctx.fillStyle = this.hovered === action.id ? action.hover : action.color;
				if (this.pressed === action.id) {
					ctx.globalAlpha = 0.72;
				}
				ctx.beginPath();
				ctx.roundRect(x, buttonY, buttonWidth, buttonHeight, 6);
				ctx.fill();
				ctx.globalAlpha = 1;
				ctx.fillStyle = action.id === "open" ? "#f4fbff" : "#d9dde2";
				ctx.font = action.id === "open" ? "600 12px sans-serif" : "600 9px sans-serif";
				const label = typeof action.label === "function" ? action.label(this) : action.label;
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
					hit.run(node, this);
				}
				return Boolean(pressed);
			}
			return false;
		},
	};
}

function installImagePreviewNode(node) {
	if (getWidget(node, "jh_image_preview_actions")) {
		return;
	}

	const copyImage = async () => {
		const image = getPreviewImage(node);
		if (!image?.src) {
			toast("warn", "JH Image Preview", "Run the workflow before copying the image.");
			return;
		}
		try {
			const response = await fetch(image.src, { cache: "no-store" });
			if (!response.ok) {
				throw new Error(`Image fetch failed: ${response.status}`);
			}
			await writeImageBlobToClipboard(await response.blob());
			toast("success", "JH Image Preview", "Image copied.");
		} catch (error) {
			console.error("[JH Image Preview] Copy failed:", error);
			toast("warn", "JH Image Preview", "The image could not be copied.");
		}
	};

	const openImage = () => {
		const image = getPreviewImage(node);
		if (!image?.src) {
			toast("warn", "JH Image Preview", "Run the workflow before opening the image.");
			return;
		}
		window.open(image.src, "_blank", "noopener,noreferrer");
	};

	node.jhHotkeyWidgetName = "jh_image_preview_actions";
	node.jhHotkeyAction = openImage;
	node.addCustomWidget(makeImagePreviewActions(copyImage, openImage));
	node.size = [Math.max(node.size?.[0] ?? 420, 420), Math.max(node.size?.[1] ?? 462, 462)];
}

function makeSaveTextHotkey() {
	return {
		type: "jh_save_text_hotkey",
		name: "jh_save_text_hotkey",
		value: "Alt+Shift+S",
		options: { serialize: true },
		serialize: true,
		recording: false,
		hovered: false,
		bounds: null,
		computeSize(width) {
			return [width || 0, 34];
		},
		draw(ctx, node, width, y) {
			this.bounds = [15, y, width - 30, 24];
			ctx.save();
			ctx.fillStyle = this.hovered ? "#59436d" : "#40334d";
			ctx.beginPath();
			ctx.roundRect(this.bounds[0], y, this.bounds[2], this.bounds[3], 6);
			ctx.fill();
			ctx.fillStyle = "#d9dde2";
			ctx.font = "600 10px sans-serif";
			ctx.textAlign = "center";
			ctx.textBaseline = "middle";
			ctx.fillText(this.recording ? "SAVE HOTKEY  ·  PRESS SHORTCUT..." : `SAVE HOTKEY  ·  ${this.value || "NOT SET"}`, width / 2, y + 12.5);
			ctx.restore();
		},
		mouse(event, pos, node) {
			const inside = this.bounds && pos[0] >= this.bounds[0] && pos[0] <= this.bounds[0] + this.bounds[2] && pos[1] >= this.bounds[1] && pos[1] <= this.bounds[1] + this.bounds[3];
			if (event.type === "pointermove") {
				this.hovered = inside;
				node.setDirtyCanvas?.(true, false);
				return inside;
			}
			if (event.type === "pointerdown" && inside) {
				startHotkeyRecording(node, this);
				return true;
			}
			return false;
		},
	};
}

function parseLoraInfo(value) {
	try {
		const loras = JSON.parse(value || "[]");
		return Array.isArray(loras) ? loras : [];
	} catch {
		return [];
	}
}

function formatLoraInfo(value) {
	const loras = parseLoraInfo(value);
	if (!loras.length) {
		return "LoRA: none";
	}
	return `LoRA: ${loras.map((lora) => `${lora.name} (${lora.strength_model})`).join(", ")}`;
}

function makeLlamaImagePreviewWidget() {
	return {
		type: "jh_llama_image_preview",
		name: "jh_llama_image_preview",
		serialize: false,
		image: null,
		status: "Waiting for input image",
		computeSize(width) {
			return [width || 0, this.image ? 250 : 38];
		},
		draw(ctx, node, width, y) {
			const height = this.image ? 238 : 28;
			const left = 10;
			const drawWidth = Math.max(1, width - 20);
			ctx.save();
			ctx.fillStyle = "#111";
			ctx.fillRect(left, y, drawWidth, height);
			if (this.image) {
				const scale = Math.min(drawWidth / this.image.width, height / this.image.height);
				const imageWidth = this.image.width * scale;
				const imageHeight = this.image.height * scale;
				ctx.drawImage(this.image, left + (drawWidth - imageWidth) / 2, y + (height - imageHeight) / 2, imageWidth, imageHeight);
			} else {
				ctx.fillStyle = "#888";
				ctx.font = "12px sans-serif";
				ctx.textAlign = "center";
				ctx.textBaseline = "middle";
				ctx.fillText(this.status, width / 2, y + height / 2);
			}
			ctx.restore();
		},
	};
}

function makeLlamaModeRowWidget(node, label, toggleWidget, strengthWidget) {
	return {
		type: "jh_llama_mode_row",
		name: `jh_${toggleWidget.name}_row`,
		serialize: false,
		bounds: {},
		computeSize(width) {
			return [width || 0, 26];
		},
		draw(ctx, node, width, y) {
			const x = 12;
			const rowWidth = width - 24;
			const strengthWidth = 92;
			const toggleWidth = 58;
			const controlX = x + rowWidth - strengthWidth - toggleWidth;
			this.bounds.row = [x, y, rowWidth, 22];
			this.bounds.dec = [controlX, y, 18, 22];
			this.bounds.strength = [controlX + 18, y, strengthWidth - 36, 22];
			this.bounds.inc = [controlX + strengthWidth - 18, y, 18, 22];
			this.bounds.toggle = [x + rowWidth - toggleWidth, y, toggleWidth, 22];
			ctx.save();
			ctx.fillStyle = "#202020";
			ctx.strokeStyle = "#666";
			ctx.beginPath();
			ctx.roundRect(x, y, rowWidth, 22, 7);
			ctx.fill();
			ctx.stroke();
			ctx.fillStyle = "#ddd";
			ctx.font = "12px sans-serif";
			ctx.textBaseline = "middle";
			ctx.textAlign = "left";
			ctx.fillText(label, x + 8, y + 11);
			ctx.textAlign = "center";
			ctx.fillText("◀", this.bounds.dec[0] + 9, y + 11);
			ctx.fillText(Number(strengthWidget.value).toFixed(2), this.bounds.strength[0] + this.bounds.strength[2] / 2, y + 11);
			ctx.fillText("▶", this.bounds.inc[0] + 9, y + 11);
			ctx.fillStyle = toggleWidget.value ? "#91a9d0" : "#666";
			ctx.beginPath();
			ctx.arc(this.bounds.toggle[0] + 11, y + 11, 6, 0, Math.PI * 2);
			ctx.fill();
			ctx.fillStyle = toggleWidget.value ? "#ddd" : "#888";
			ctx.textAlign = "left";
			ctx.fillText(toggleWidget.value ? "On" : "Off", this.bounds.toggle[0] + 22, y + 11);
			ctx.restore();
		},
		mouse(event, pos) {
			const contains = (bounds) => bounds && pos[0] >= bounds[0] && pos[0] <= bounds[0] + bounds[2] && pos[1] >= bounds[1] && pos[1] <= bounds[1] + bounds[3];
			if (event.type !== "pointerdown" || !contains(this.bounds.row)) {
				return false;
			}
			if (contains(this.bounds.toggle)) {
				setWidgetValue(node, toggleWidget, !toggleWidget.value);
			} else if (contains(this.bounds.dec)) {
				setWidgetValue(node, strengthWidget, Math.max(0.25, Math.round((Number(strengthWidget.value) - 0.05) * 100) / 100));
			} else if (contains(this.bounds.inc)) {
				setWidgetValue(node, strengthWidget, Math.min(3, Math.round((Number(strengthWidget.value) + 0.05) * 100) / 100));
			} else if (contains(this.bounds.strength)) {
				app.canvas.prompt(`${label} strength`, strengthWidget.value, (value) => {
					const strength = Number(value);
					if (Number.isFinite(strength)) {
						setWidgetValue(node, strengthWidget, Math.max(0.25, Math.min(3, strength)));
					}
				}, event);
			}
			return true;
		},
	};
}

function installLlamaImagePreview(node) {
	for (const [name, label] of [
		["character_sheet_mode", "prompt output mode"],
		["character_identity", "character identity"],
		["identity_trigger", "identity trigger"],
		["curvy_mode", "curvy mode"],
		["glamorous_mode", "glamorous mode"],
		["huge_breasts_mode", "huge breasts mode"],
		["reuse_identical_image", "reuse identical image"],
	]) {
		const widget = getWidget(node, name);
		if (widget) widget.label = label;
	}
	const outputModeWidget = getWidget(node, "character_sheet_mode");
	if (outputModeWidget && typeof outputModeWidget.value === "boolean") {
		setWidgetValue(node, outputModeWidget, outputModeWidget.value ? "Character sheet" : "Normal");
	}
	const identityTriggerWidget = getWidget(node, "identity_trigger");
	if (identityTriggerWidget && ["", "none", "null", "undefined"].includes(String(identityTriggerWidget.value ?? "").trim().toLowerCase())) {
		setWidgetValue(node, identityTriggerWidget, "");
	}
	const reuseIdenticalImageWidget = getWidget(node, "reuse_identical_image");
	if (reuseIdenticalImageWidget && typeof reuseIdenticalImageWidget.value !== "boolean") {
		setWidgetValue(node, reuseIdenticalImageWidget, false);
	}
	if (!node.jhLlamaModeRowsInstalled) {
		const modes = [
			["curvy mode", "curvy_mode", "curvy_strength"],
			["glamorous mode", "glamorous_mode", "glamorous_strength"],
			["huge breasts mode", "huge_breasts_mode", "huge_breasts_strength"],
		];
		const widgets = modes.map(([label, toggleName, strengthName]) => [label, getWidget(node, toggleName), getWidget(node, strengthName)]);
		if (widgets.every(([, toggleWidget, strengthWidget]) => toggleWidget && strengthWidget)) {
			node.jhLlamaModeRowsInstalled = true;
			const normalizeStrengths = () => {
				for (const [, , strengthWidget] of widgets) {
					if (!Number.isFinite(Number(strengthWidget.value))) {
						setWidgetValue(node, strengthWidget, 1);
					}
				}
			};
			normalizeStrengths();
			for (const [label, toggleWidget, strengthWidget] of widgets) {
				hideWidget(toggleWidget);
				hideWidget(strengthWidget);
				node.addCustomWidget(makeLlamaModeRowWidget(node, label, toggleWidget, strengthWidget));
			}
			const originalOnConfigure = node.onConfigure;
			node.onConfigure = function () {
				originalOnConfigure?.apply(this, arguments);
				requestAnimationFrame(normalizeStrengths);
			};
		}
	}
	if (getWidget(node, "jh_llama_image_preview")) {
		return;
	}
	node.addCustomWidget(makeLlamaImagePreviewWidget());
	requestAnimationFrame(() => {
		const size = node.computeSize?.();
		if (size) {
			node.setSize?.([Math.max(node.size[0], size[0]), Math.max(node.size[1], size[1])]);
		}
	});
}

api.addEventListener("jh-llama-image-preview", ({ detail }) => {
	const numericId = Number(detail?.node_id);
	const node = app.graph?.getNodeById?.(Number.isNaN(numericId) ? detail?.node_id : numericId);
	if (!node || node.comfyClass !== "JHLlamaPrompt" || !detail?.image) {
		return;
	}
	installLlamaImagePreview(node);
	const widget = getWidget(node, "jh_llama_image_preview");
	widget.image = null;
	widget.status = "Loading input preview...";
	const preview = new Image();
	preview.onload = () => {
		widget.image = preview;
		const size = node.computeSize?.();
		if (size) {
			node.setSize?.([Math.max(node.size[0], size[0]), Math.max(node.size[1], size[1])]);
		}
		node.setDirtyCanvas?.(true, true);
		app.graph?.setDirtyCanvas?.(true, true);
	};
	preview.src = detail.image;
	node.setDirtyCanvas?.(true, true);
});

api.addEventListener("jh-llama-cache-hit", ({ detail }) => {
	toast("info", "JH llama.cpp Prompt", detail?.message || "Reused cached prompt.");
});

function makeAutoFeedPreviewWidget() {
	return {
		type: "jh_auto_feed_preview",
		name: "jh_auto_feed_preview",
		serialize: false,
		inputImage: null,
		outputImage: null,
		metadata: null,
		status: "Waiting for crawled media",
		linkBounds: null,
		hoveredLink: false,
		computeSize(width) {
			return [width || 0, this.inputImage && this.outputImage ? 400 : 52];
		},
		draw(ctx, node, width, y) {
			const left = 10;
			const drawWidth = Math.max(1, width - 20);
			ctx.save();
			ctx.fillStyle = "#111";
			ctx.fillRect(left, y, drawWidth, this.inputImage && this.outputImage ? 390 : 44);
			if (!this.inputImage || !this.outputImage) {
				ctx.fillStyle = "#888";
				ctx.font = "12px sans-serif";
				ctx.textAlign = "center";
				const status = String(this.status || "Waiting for crawled media");
				let line = status;
				while (ctx.measureText(`${line}...`).width > drawWidth - 12 && line.length > 12) line = line.slice(0, -1);
				ctx.fillText(line === status ? line : `${line}...`, width / 2, y + 25);
				ctx.restore();
				return;
			}
			const imageHeight = 245;
			const labelHeight = 20;
			const gap = 6;
			const cellWidth = (drawWidth - gap) / 2;
			const drawCell = (image, x, label) => {
				ctx.fillStyle = "#1b1b1b";
				ctx.fillRect(x, y, cellWidth, imageHeight);
				ctx.fillStyle = "#aaa";
				ctx.font = "11px sans-serif";
				ctx.textAlign = "center";
				ctx.fillText(label, x + cellWidth / 2, y + 14);
				const availableHeight = imageHeight - labelHeight;
				const scale = Math.min(cellWidth / image.width, availableHeight / image.height);
				const renderedWidth = image.width * scale;
				const renderedHeight = image.height * scale;
				ctx.drawImage(image, x + (cellWidth - renderedWidth) / 2, y + labelHeight + (availableHeight - renderedHeight) / 2, renderedWidth, renderedHeight);
			};
			drawCell(this.inputImage, left, "Before Crop");
			drawCell(this.outputImage, left + cellWidth + gap, "Final Output");
			const meta = this.metadata || {};
			const size = Array.isArray(meta.output_size) ? `${meta.output_size[0]}×${meta.output_size[1]}` : "";
			const score = Number.isFinite(meta.woman_subject_score) ? `subject ${meta.woman_subject_score.toFixed(3)}` : "";
			const faceConfidence = Number.isFinite(meta.face_confidence) ? `face ${meta.face_confidence.toFixed(3)}` : "";
			const popularityName = meta.source?.startsWith("Instagram") ? "likes" : meta.source === "X Search" ? "likes" : meta.source === "DCInside Gallery" ? "recommends" : meta.source === "Reddit Subreddit" ? "post score" : "";
			const popularityValue = meta.source?.startsWith("Instagram") ? meta.like_count : meta.rank_score;
			const popularity = popularityName && Number.isFinite(popularityValue) ? `${popularityName} ${popularityValue}` : "";
			const commentsName = meta.source === "X Search" ? "replies" : meta.source === "DCInside Gallery" || meta.source === "Reddit Subreddit" || meta.source?.startsWith("Instagram") ? "comments" : "";
			const comments = commentsName && Number.isFinite(meta.comment_count) ? `${commentsName} ${meta.comment_count}` : "";
			const views = meta.source === "DCInside Gallery" && Number.isFinite(meta.view_count) ? `views ${meta.view_count}` : "";
			const frame = Number.isFinite(meta.frame_time) ? `frame ${meta.frame_time.toFixed(2)}s (#${meta.frame_index || 0})` : "";
			const sourceDate = [meta.source || "", meta.post_date ? `posted ${meta.post_date}` : ""].filter(Boolean).join("  ·  ");
			const lines = [
				{ text: `${meta.media_type || "image"}  ${meta.media_extension || ""}  ${size}`.trim() },
				{ text: [score, faceConfidence, popularity, comments, views, frame].filter(Boolean).join("  ") },
				{ text: sourceDate },
				{ text: meta.title || "" },
				{ text: meta.page_url || "", link: true },
			].filter(line => line.text);
			ctx.fillStyle = "#bbb";
			ctx.font = "11px monospace";
			ctx.textAlign = "left";
			this.linkBounds = null;
			for (let index = 0; index < lines.length; index++) {
				const original = String(lines[index].text);
				let line = original;
				while (ctx.measureText(line).width > drawWidth - 12 && line.length > 12) line = line.slice(0, -1);
				if (line !== original) line += "…";
				const lineY = y + imageHeight + 20 + index * 18;
				ctx.fillStyle = lines[index].link ? (this.hoveredLink ? "#8fc7ff" : "#69aee8") : "#bbb";
				ctx.fillText(line, left + 6, lineY);
				if (lines[index].link) {
					const linkWidth = Math.min(ctx.measureText(line).width, drawWidth - 12);
					this.linkBounds = [left + 6, lineY - 12, linkWidth, 16];
					ctx.fillRect(left + 6, lineY + 2, linkWidth, 1);
				}
			}
			ctx.fillStyle = "#777";
			ctx.fillRect(left + 6, y + 356, drawWidth - 12, 1);
			ctx.fillStyle = "#e0b35a";
			const originalStatus = String(this.status || "");
			let status = originalStatus;
			while (ctx.measureText(`${status}...`).width > drawWidth - 12 && status.length > 12) status = status.slice(0, -1);
			ctx.fillText(status === originalStatus ? status : `${status}...`, left + 6, y + 378);
			ctx.restore();
		},
		mouse(event, pos, node) {
			const bounds = this.linkBounds;
			const inside = bounds && pos[0] >= bounds[0] && pos[0] <= bounds[0] + bounds[2] && pos[1] >= bounds[1] && pos[1] <= bounds[1] + bounds[3];
			if (event.type === "pointermove") {
				this.hoveredLink = Boolean(inside);
				node.setDirtyCanvas?.(true, false);
				return Boolean(inside);
			}
			if (event.type === "pointerdown" && inside) {
				const url = new URL(this.metadata.page_url);
				if (url.protocol === "http:" || url.protocol === "https:") window.open(url.href, "_blank", "noopener,noreferrer");
				return true;
			}
			return false;
		},
	};
}

function installAutoFeedPreview(node) {
	if (!getWidget(node, "jh_auto_feed_preview")) {
		node.addCustomWidget(makeAutoFeedPreviewWidget());
	}
}

api.addEventListener("jh-auto-feed-preview", ({ detail }) => {
	const numericId = Number(detail?.node_id);
	const node = app.graph?.getNodeById?.(Number.isNaN(numericId) ? detail?.node_id : numericId);
	if (!node || node.comfyClass !== "JHAutoImageFeed" || !detail?.input_image || !detail?.output_image) return;
	installAutoFeedPreview(node);
	const widget = getWidget(node, "jh_auto_feed_preview");
	const previewToken = (widget.previewToken || 0) + 1;
	widget.previewToken = previewToken;
	const inputPreview = new Image();
	const outputPreview = new Image();
	let loaded = 0;
	const finish = () => {
		loaded += 1;
		if (loaded < 2) return;
		inputPreview.onload = null;
		outputPreview.onload = null;
		if (widget.previewToken !== previewToken) return;
		widget.inputImage = inputPreview;
		widget.outputImage = outputPreview;
		widget.metadata = detail.metadata || {};
		widget.status = "Accepted media. Ready for the next run.";
		node.jhRefreshAutoFeedPresets?.();
		const size = node.computeSize?.();
		if (size) node.setSize?.([Math.max(node.size[0], size[0]), size[1]]);
		node.setDirtyCanvas?.(true, true);
		app.graph?.setDirtyCanvas?.(true, true);
	};
	inputPreview.onload = finish;
	outputPreview.onload = finish;
	inputPreview.src = detail.input_image;
	outputPreview.src = detail.output_image;
});

api.addEventListener("jh-auto-feed-status", ({ detail }) => {
	const numericId = Number(detail?.node_id);
	const node = app.graph?.getNodeById?.(Number.isNaN(numericId) ? detail?.node_id : numericId);
	if (!node || node.comfyClass !== "JHAutoImageFeed") return;
	installAutoFeedPreview(node);
	const widget = getWidget(node, "jh_auto_feed_preview");
	widget.status = detail?.status || "Waiting for crawled media";
	if (widget.statusTimer) return;
	widget.statusTimer = setTimeout(() => {
		widget.statusTimer = null;
		node.setDirtyCanvas?.(true, false);
	}, 150);
});

api.addEventListener("jh-auto-feed-presets-updated", () => {
	for (const node of app.graph?._nodes || []) {
		if (node.comfyClass === "JHAutoImageFeed") node.jhRefreshAutoFeedPresets?.();
	}
});

function installShowTextNode(node) {
	const storedWidget = getWidget(node, "display_text");
	const storedLoraWidget = getWidget(node, "display_lora_info");
	hideWidget(storedWidget);
	hideWidget(storedLoraWidget);

	let textWidget = getWidget(node, "jh_output_text");
	if (!textWidget) {
		textWidget = ComfyWidgets["STRING"](node, "jh_output_text", ["STRING", { multiline: true }], app).widget;
		textWidget.value = storedWidget?.value || "";
	}
	if (textWidget.inputEl) {
		textWidget.inputEl.readOnly = true;
		textWidget.inputEl.style.opacity = 0.75;
	}
	textWidget.serialize = false;

	let loraWidget = getWidget(node, "jh_output_loras");
	if (!loraWidget) {
		loraWidget = ComfyWidgets["STRING"](node, "jh_output_loras", ["STRING", {}], app).widget;
		loraWidget.value = formatLoraInfo(storedLoraWidget?.value);
	}
	if (loraWidget.inputEl) {
		loraWidget.inputEl.readOnly = true;
		loraWidget.inputEl.style.opacity = 0.75;
	}
	loraWidget.serialize = false;

	node.jhSaveText = async () => {
		const text = textWidget.value || "";
		if (!text.trim()) {
			toast("warn", "Saved Texts", "There is no text to save.");
			return;
		}
		try {
			const response = await api.fetchApi("/jh/saved-texts", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ text, loras: parseLoraInfo(storedLoraWidget?.value) }),
			});
			const result = await response.json();
			if (!response.ok) {
				throw new Error(result.error || `Save failed: ${response.status}`);
			}
			toast(result.added ? "success" : "info", "Saved Texts", result.added ? `Text saved. ${result.count} total.` : "This text is already saved.");
		} catch (error) {
			console.error("[JH Show Text] Text save failed:", error);
			toast("warn", "Saved Texts", error.message || "Text could not be saved.");
		}
	};

	if (!getWidget(node, "Save Text")) {
		const saveButton = node.addWidget("button", "Save Text", "Save Text", node.jhSaveText, { serialize: false });
		saveButton.serialize = false;
	}

	if (!getWidget(node, "Copy Text")) {
		const copyButton = node.addWidget("button", "Copy Text", "Copy Text", async () => {
			try {
				await navigator.clipboard.writeText(textWidget.value || "");
				toast("success", "Clipboard", "Text copied.");
			} catch (error) {
				console.error("[JH Show Text] Clipboard copy failed:", error);
				toast("warn", "Clipboard", "Text could not be copied.");
			}
		}, { serialize: false });
		copyButton.serialize = false;
	}

	node.jhHotkeyWidgetName = "jh_save_text_hotkey";
	node.jhHotkeyAction = node.jhSaveText;
	if (!getWidget(node, "jh_save_text_hotkey")) {
		node.addCustomWidget(makeSaveTextHotkey());
	}

	if (!node.jhShowTextHandlersInstalled) {
		node.jhShowTextHandlersInstalled = true;
		const originalOnExecuted = node.onExecuted;
		node.onExecuted = function (message) {
			originalOnExecuted?.apply(this, arguments);
			const value = Array.isArray(message?.text) ? message.text.join("\n") : (message?.text ?? "");
			const loraInfo = Array.isArray(message?.lora_info) ? message.lora_info[0] : (message?.lora_info ?? "[]");
			setWidgetValue(this, getWidget(this, "jh_output_text"), value);
			setWidgetValue(this, getWidget(this, "display_text"), value);
			setWidgetValue(this, getWidget(this, "display_lora_info"), loraInfo);
			setWidgetValue(this, getWidget(this, "jh_output_loras"), formatLoraInfo(loraInfo));
		};

		const originalOnConfigure = node.onConfigure;
		node.onConfigure = function () {
			const configuredNode = arguments[0];
			originalOnConfigure?.apply(this, arguments);
			requestAnimationFrame(() => {
				const restoredWidget = getWidget(this, "display_text");
				const restoredLoraWidget = getWidget(this, "display_lora_info");
				const metadataText = configuredNode?.properties?.jh_llama_prompt_output;
				const restoredText = this.comfyClass === "JHLlamaPrompt" && typeof metadataText === "string" ? metadataText : (restoredWidget?.value || "");
				hideWidget(restoredWidget);
				hideWidget(restoredLoraWidget);
				setWidgetValue(this, restoredWidget, restoredText);
				setWidgetValue(this, getWidget(this, "jh_output_text"), restoredText);
				setWidgetValue(this, getWidget(this, "jh_output_loras"), formatLoraInfo(restoredLoraWidget?.value));
			});
		};
	}
	requestAnimationFrame(() => {
		const size = node.computeSize?.();
		if (size) {
			node.onResize?.([Math.max(node.size[0], size[0]), Math.max(node.size[1], size[1])]);
		}
		node.setDirtyCanvas?.(true, true);
	});
}

function installSavedTextPickerNode(node) {
	if (getWidget(node, "jh_picked_text")) {
		return;
	}
	const statusWidget = ComfyWidgets["STRING"](node, "jh_pick_status", ["STRING", {}], app).widget;
	statusWidget.inputEl.readOnly = true;
	statusWidget.inputEl.style.opacity = 0.75;
	statusWidget.serialize = false;
	const loraWidget = ComfyWidgets["STRING"](node, "jh_picked_loras", ["STRING", {}], app).widget;
	loraWidget.inputEl.readOnly = true;
	loraWidget.inputEl.style.opacity = 0.75;
	loraWidget.serialize = false;
	const textWidget = ComfyWidgets["STRING"](node, "jh_picked_text", ["STRING", { multiline: true }], app).widget;
	textWidget.inputEl.readOnly = true;
	textWidget.inputEl.style.opacity = 0.75;
	textWidget.serialize = false;

	const originalOnExecuted = node.onExecuted;
	node.onExecuted = function (message) {
		originalOnExecuted?.apply(this, arguments);
		const text = Array.isArray(message?.text) ? message.text.join("\n") : (message?.text ?? "");
		const status = Array.isArray(message?.status) ? message.status[0] : (message?.status ?? "");
		const loraInfo = Array.isArray(message?.lora_info) ? message.lora_info[0] : (message?.lora_info ?? "[]");
		setWidgetValue(this, textWidget, text);
		setWidgetValue(this, statusWidget, status);
		setWidgetValue(this, loraWidget, formatLoraInfo(loraInfo));
	};
}

function installPriorityPassthroughNode(node) {
	if (getWidget(node, "jh_priority_info")) {
		node.jhUpdatePrioritySelectorLabel?.();
		return;
	}
	const selectorWidget = getWidget(node, "selected_input");
	if (selectorWidget) {
		const updateSelectorLabel = () => {
			const selectedInput = Math.max(0, Math.trunc(Number(selectorWidget.value) || 0));
			selectorWidget.label = selectedInput === 0
				? "selector · 0 (Auto)"
				: `selector · ${selectedInput} (input${selectedInput})`;
		};
		node.jhUpdatePrioritySelectorLabel = updateSelectorLabel;
		const originalSelectorCallback = selectorWidget.callback;
		selectorWidget.callback = function () {
			const result = originalSelectorCallback?.apply(this, arguments);
			updateSelectorLabel();
			return result;
		};
		updateSelectorLabel();
	}
	const bypassedInputsWidget = getWidget(node, "bypassed_inputs");
	hideWidget(bypassedInputsWidget);
	if (bypassedInputsWidget) {
		bypassedInputsWidget.serializeValue = () => {
			const bypassed = [];
			for (const input of node.inputs || []) {
				const inputName = input.name?.split(".").at(-1);
				if (!/^input\d+$/.test(inputName || "") || input.link == null) {
					continue;
				}
				const link = app.graph?.links?.[input.link];
				const originNode = link ? app.graph?.getNodeById?.(link.origin_id) : null;
				if (originNode?.mode === 4) {
					bypassed.push(inputName);
				}
			}
			return JSON.stringify(bypassed);
		};
	}
	const infoWidget = ComfyWidgets["STRING"](node, "jh_priority_info", ["STRING", { multiline: true }], app).widget;
	infoWidget.label = "Input / Output Values";
	infoWidget.inputEl.readOnly = true;
	infoWidget.inputEl.style.opacity = 0.75;
	infoWidget.serialize = false;

	const originalOnExecuted = node.onExecuted;
	node.onExecuted = function (message) {
		originalOnExecuted?.apply(this, arguments);
		const info = Array.isArray(message?.priority_info) ? message.priority_info.join("\n") : (message?.priority_info ?? "");
		setWidgetValue(this, infoWidget, info);
		requestAnimationFrame(() => {
			const size = this.computeSize?.();
			if (size) {
				this.onResize?.([Math.max(this.size[0], size[0]), Math.max(this.size[1], size[1])]);
			}
		});
	};
}

function migrateSavedPickerNode(node) {
	if (!["JHSavedPicker", "JHSavedTextPicker", "JHSavedTextLoraPicker"].includes(node.comfyClass)) {
		return;
	}
	if (["JH Saved Text Picker", "JH Saved Text + LoRA Picker"].includes(node.title)) {
		node.title = "JH Saved Picker";
	}
	if (!node.inputs?.some((input) => input.name === "model")) {
		node.addInput("model", "MODEL");
	}
	if (!node.inputs?.some((input) => input.name === "clip")) {
		node.addInput("clip", "CLIP");
	}
	if (!node.outputs?.some((output) => output.name === "model")) {
		node.addOutput("model", "MODEL");
	}
	if (!node.outputs?.some((output) => output.name === "clip")) {
		node.addOutput("clip", "CLIP");
	}
}

let loraNamesPromise = null;

function getLoraNames(forceRefresh = false) {
	if (forceRefresh) {
		loraNamesPromise = null;
	}
	if (!loraNamesPromise) {
		loraNamesPromise = api.fetchApi(`/jh/loras?refresh=${Date.now()}`, { cache: "no-store" })
			.then((response) => {
				if (!response.ok) {
					throw new Error(`LoRA list failed: ${response.status}`);
				}
				return response.json();
			})
			.then((result) => result.loras || []);
	}
	return loraNamesPromise;
}

function parseAdditionalLoras(value) {
	try {
		const loras = JSON.parse(value || "[]");
		return Array.isArray(loras) ? loras : [];
	} catch {
		return [];
	}
}

function showJHLoraChooser(loraNames, currentValue, onChoose) {
	const overlay = document.createElement("div");
	overlay.style.cssText = "position:fixed;inset:0;z-index:100000;background:#0008;display:flex;align-items:center;justify-content:center";
	const panel = document.createElement("div");
	panel.style.cssText = "width:min(680px,85vw);height:min(620px,80vh);background:#252525;border:1px solid #666;border-radius:10px;padding:12px;display:flex;flex-direction:column;gap:8px;box-shadow:0 16px 48px #000";
	const search = document.createElement("input");
	search.placeholder = "Search LoRA...";
	search.autofocus = true;
	search.value = currentValue === "None" ? "" : currentValue;
	search.style.cssText = "padding:9px;background:#111;color:#eee;border:1px solid #666;border-radius:6px";
	const list = document.createElement("div");
	list.style.cssText = "overflow:auto;display:flex;flex-direction:column;gap:2px";
	const close = () => overlay.remove();
	const render = () => {
		const query = search.value.toLowerCase();
		const matches = ["None", ...loraNames].filter((name) => name.toLowerCase().includes(query)).slice(0, 300);
		list.replaceChildren(...matches.map((name) => {
			const button = document.createElement("button");
			button.textContent = name;
			button.style.cssText = `text-align:left;padding:7px 9px;border:0;border-radius:4px;color:#eee;background:${name === currentValue ? "#506685" : "#333"};cursor:pointer`;
			button.onclick = () => {
				onChoose(name);
				close();
			};
			return button;
		}));
	};
	search.addEventListener("input", render);
	overlay.addEventListener("pointerdown", (event) => {
		if (event.target === overlay) {
			close();
		}
	});
	overlay.addEventListener("keydown", (event) => {
		if (event.key === "Escape") {
			close();
		}
	});
	panel.append(search, list);
	overlay.append(panel);
	document.body.append(overlay);
	render();
	requestAnimationFrame(() => {
		search.focus({ preventScroll: true });
		search.select();
	});
	setTimeout(() => search.focus({ preventScroll: true }), 0);
}

function fitCanvasText(ctx, text, maxWidth) {
	if (ctx.measureText(text).width <= maxWidth) {
		return text;
	}
	let value = text;
	while (value.length > 4 && ctx.measureText(`${value}...`).width > maxWidth) {
		value = value.slice(0, -1);
	}
	return `${value}...`;
}

function drawLoraToggle(ctx, x, y, height, enabled) {
	const width = height * 1.5;
	ctx.save();
	ctx.beginPath();
	ctx.roundRect(x + 4, y + 4, width - 8, height - 8, height * 0.5);
	ctx.globalAlpha = app.canvas.editor_alpha * 0.25;
	ctx.fillStyle = "rgba(255,255,255,0.45)";
	ctx.fill();
	ctx.globalAlpha = app.canvas.editor_alpha;
	ctx.fillStyle = enabled ? "#89B" : "#888";
	ctx.beginPath();
	ctx.arc(x + (enabled ? height : height * 0.5), y + height * 0.5, height * 0.36, 0, Math.PI * 2);
	ctx.fill();
	ctx.restore();
	return width;
}

function makeLoraRowWidget(value, onChange, onRemove) {
	return {
		type: "jh_lora_row",
		name: `jh_lora_row_${Math.random().toString(36).slice(2)}`,
		value: {
			enabled: value.enabled !== false,
			name: value.name || "None",
			strength: Number(value.strength_model ?? 1),
		},
		serialize: false,
		bounds: {},
		computeSize(width) {
			return [width || 0, 24];
		},
		draw(ctx, node, width, y) {
			const x = 12;
			const rowWidth = width - 24;
			const strengthWidth = 82;
			this.bounds.row = [x, y, rowWidth, 20];
			this.bounds.toggle = [x, y, 30, 20];
			this.bounds.name = [x + 34, y, rowWidth - strengthWidth - 39, 20];
			this.bounds.dec = [x + rowWidth - strengthWidth, y, 18, 20];
			this.bounds.strength = [x + rowWidth - strengthWidth + 18, y, strengthWidth - 36, 20];
			this.bounds.inc = [x + rowWidth - 18, y, 18, 20];
			ctx.save();
			ctx.fillStyle = "#202020";
			ctx.strokeStyle = "#666";
			ctx.beginPath();
			ctx.roundRect(x, y, rowWidth, 20, 7);
			ctx.fill();
			ctx.stroke();
			drawLoraToggle(ctx, x, y, 20, this.value.enabled);
			ctx.globalAlpha = this.value.enabled ? 1 : 0.45;
			ctx.fillStyle = "#ddd";
			ctx.font = "12px sans-serif";
			ctx.textAlign = "left";
			ctx.textBaseline = "middle";
			ctx.fillText(fitCanvasText(ctx, this.value.name, this.bounds.name[2]), this.bounds.name[0], y + 10);
			ctx.textAlign = "center";
			ctx.fillText("◀", this.bounds.dec[0] + 9, y + 10);
			ctx.fillText(this.value.strength.toFixed(2), this.bounds.strength[0] + this.bounds.strength[2] / 2, y + 10);
			ctx.fillText("▶", this.bounds.inc[0] + 9, y + 10);
			ctx.restore();
		},
		mouse(event, pos, node) {
			const contains = (bounds) => bounds && pos[0] >= bounds[0] && pos[0] <= bounds[0] + bounds[2] && pos[1] >= bounds[1] && pos[1] <= bounds[1] + bounds[3];
			if (event.type !== "pointerdown" || !contains(this.bounds.row)) {
				return false;
			}
			if (event.button === 2) {
				new LiteGraph.ContextMenu([{ content: "Remove LoRA", callback: () => onRemove() }], { title: "LoRA", event });
				return true;
			}
			if (contains(this.bounds.toggle)) {
				this.value.enabled = !this.value.enabled;
			} else if (contains(this.bounds.dec)) {
				this.value.strength = Math.round((this.value.strength - 0.05) * 100) / 100;
			} else if (contains(this.bounds.inc)) {
				this.value.strength = Math.round((this.value.strength + 0.05) * 100) / 100;
			} else if (contains(this.bounds.strength)) {
				app.canvas.prompt("LoRA strength", this.value.strength, (value) => {
					this.value.strength = Number(value);
					onChange();
					node.setDirtyCanvas?.(true, true);
				}, event);
				return true;
			} else {
				getLoraNames(true).then((loraNames) => {
					showJHLoraChooser(loraNames, this.value.name, (name) => {
						this.value.name = name;
						onChange();
						node.setDirtyCanvas?.(true, true);
					});
				}).catch((error) => {
					console.error("[JH LoRA Stack] LoRA refresh failed:", error);
					toast("warn", "JH LoRA", "The LoRA list could not be refreshed.");
				});
				return true;
			}
			onChange();
			node.setDirtyCanvas?.(true, true);
			return true;
		},
	};
}

function makeLoraHeaderWidget(node, onChange) {
	return {
		type: "jh_lora_header",
		name: "jh_lora_header",
		serialize: false,
		bounds: null,
		computeSize(width) {
			return [width || 0, 22];
		},
		draw(ctx, node, width, y) {
			this.bounds = [12, y, width - 24, 18];
			const allEnabled = node.jhLoraSlots.length > 0 && node.jhLoraSlots.every((slot) => slot.value.enabled);
			ctx.save();
			drawLoraToggle(ctx, 12, y, 18, allEnabled);
			ctx.fillStyle = "#aaa";
			ctx.font = "12px sans-serif";
			ctx.textBaseline = "middle";
			ctx.textAlign = "left";
			ctx.fillText("Toggle All", 42, y + 9);
			ctx.textAlign = "right";
			ctx.fillText("LoRA Strength", width - 17, y + 9);
			ctx.restore();
		},
		mouse(event, pos, node) {
			const inside = this.bounds && pos[0] >= this.bounds[0] && pos[0] <= this.bounds[0] + this.bounds[2] && pos[1] >= this.bounds[1] && pos[1] <= this.bounds[1] + this.bounds[3];
			if (event.type !== "pointerdown" || !inside) {
				return false;
			}
			const enabled = !node.jhLoraSlots.every((slot) => slot.value.enabled);
			for (const slot of node.jhLoraSlots) {
				slot.value.enabled = enabled;
			}
			onChange();
			node.setDirtyCanvas?.(true, true);
			return true;
		},
	};
}

async function installLoraStackNode(node) {
	if (node.jhLoraStackInstalled) {
		return;
	}
	node.jhLoraStackInstalled = true;
	const storedWidget = getWidget(node, "additional_loras");
	if (!storedWidget) {
		return;
	}
	hideWidget(storedWidget);

	node.jhLoraSlots = [];
	const resizeNode = () => {
		const size = node.computeSize?.();
		if (size) {
			node.onResize?.([Math.max(node.size[0], size[0]), size[1]]);
		}
		node.setDirtyCanvas?.(true, true);
	};
	const syncSlots = () => {
		const value = node.jhLoraSlots.map((slot) => ({
			enabled: slot.value.enabled,
			name: slot.value.name,
			strength_model: slot.value.strength,
			strength_clip: slot.value.strength,
		}));
		setWidgetValue(node, storedWidget, JSON.stringify(value));
	};
	const detachWidget = (widget) => {
		const index = node.widgets?.indexOf(widget) ?? -1;
		if (index < 0) {
			return false;
		}
		node.widgets.splice(index, 1);
		widget.onRemove?.();
		return true;
	};
	const removeSlot = (slot) => {
		detachWidget(slot);
		node.jhLoraSlots = node.jhLoraSlots.filter((item) => item !== slot);
		syncSlots();
		resizeNode();
	};
	const originalGetSlotInPosition = node.getSlotInPosition?.bind(node);
	const originalGetSlotMenuOptions = node.getSlotMenuOptions?.bind(node);
	node.getSlotInPosition = function (canvasX, canvasY) {
		const slot = originalGetSlotInPosition?.(canvasX, canvasY);
		if (slot || canvasX < this.pos[0] || canvasX > this.pos[0] + this.size[0]) {
			return slot;
		}
		for (const widget of this.jhLoraSlots || []) {
			const top = this.pos[1] + (widget.last_y ?? -1000);
			if (canvasY >= top && canvasY <= top + 24) {
				return { widget, output: { type: "JH_LORA_ROW" } };
			}
		}
		return slot;
	};
	node.getSlotMenuOptions = function (slot) {
		if (slot?.widget?.type === "jh_lora_row") {
			return [{
				content: "Remove LoRA",
				callback: () => removeSlot(slot.widget),
			}];
		}
		return originalGetSlotMenuOptions?.(slot);
	};
	const moveBeforeAddButton = (widget) => {
		const widgetIndex = node.widgets.indexOf(widget);
		const buttonIndex = node.widgets.indexOf(addButton);
		if (widgetIndex > buttonIndex && buttonIndex >= 0) {
			node.widgets.splice(widgetIndex, 1);
			node.widgets.splice(buttonIndex, 0, widget);
		}
	};
	const addSlot = (data = {}, sync = true) => {
		let slot;
		slot = makeLoraRowWidget(data, syncSlots, () => removeSlot(slot));
		node.addCustomWidget(slot);
		node.jhLoraSlots.push(slot);
		moveBeforeAddButton(slot);
		if (sync) {
			syncSlots();
		}
		resizeNode();
	};
	const rebuildSlots = () => {
		for (const slot of node.jhLoraSlots) {
			detachWidget(slot);
		}
		node.jhLoraSlots = [];
		for (const data of parseAdditionalLoras(storedWidget.value)) {
			addSlot(data, false);
		}
		resizeNode();
	};
	const addButton = node.addWidget("button", "+ Add LoRA", "+ Add LoRA", () => addSlot(), { serialize: false });
	addButton.serialize = false;
	const headerWidget = node.addCustomWidget(makeLoraHeaderWidget(node, syncSlots));
	moveBeforeAddButton(headerWidget);

	const originalOnConfigure = node.onConfigure;
	node.onConfigure = function () {
		const configuredValues = arguments[0]?.widgets_values;
		originalOnConfigure?.apply(this, arguments);
		requestAnimationFrame(() => {
			const configuredWidget = getWidget(this, "additional_loras");
			if (Array.isArray(configuredValues) && configuredValues.length >= 3 && typeof configuredValues[0] === "string" && typeof configuredValues[1] === "number" && !configuredValues[0].trim().startsWith("[")) {
				const migrated = [{
					enabled: configuredValues[1] !== 0,
					name: configuredValues[0],
					strength_model: configuredValues[1],
					strength_clip: configuredValues[1],
				}, ...parseAdditionalLoras(configuredValues[3])];
				configuredWidget.value = JSON.stringify(migrated);
			}
			hideWidget(configuredWidget);
			rebuildSlots();
		});
	};
	rebuildSlots();
}


function makeTextClipboardActions(onPaste, onPasteAndRun) {
	const actions = [
		{ id: "run", label: "PASTE & RUN", color: "#176b87", hover: "#2187a8", run: onPasteAndRun },
		{ id: "paste", label: "PASTE ONLY", color: "#3b4654", hover: "#526173", run: onPaste },
		{ id: "hotkey", label: (widget) => widget.recording ? "PRESS SHORTCUT..." : `HOTKEY  ·  ${widget.value || "NOT SET"}`, color: "#40334d", hover: "#59436d", run: startHotkeyRecording },
	];

	return {
		type: "jh_text_clipboard_actions",
		name: "jh_text_clipboard_hotkey",
		value: "Alt+Shift+T",
		options: { serialize: true },
		serialize: true,
		recording: false,
		pressed: null,
		hovered: null,
		bounds: {},
		computeSize(width) {
			return [width || 0, 110];
		},
		draw(ctx, node, width, y) {
			const margin = 15;
			const innerWidth = width - margin * 2;
			const pasteWidth = Math.floor(innerWidth * 0.48);
			const hotkeyWidth = Math.min(160, Math.floor(innerWidth * 0.55));
			this.bounds.run = [margin, y, innerWidth, 36];
			this.bounds.paste = [margin, y + 52, pasteWidth, 24];
			this.bounds.hotkey = [margin + innerWidth - hotkeyWidth, y + 84, hotkeyWidth, 18];

			ctx.save();
			ctx.textAlign = "center";
			ctx.textBaseline = "middle";
			for (const action of actions) {
				const [x, buttonY, buttonWidth, buttonHeight] = this.bounds[action.id];
				ctx.fillStyle = this.hovered === action.id ? action.hover : action.color;
				if (this.pressed === action.id) {
					ctx.globalAlpha = 0.72;
				}
				ctx.beginPath();
				ctx.roundRect(x, buttonY, buttonWidth, buttonHeight, 6);
				ctx.fill();
				ctx.globalAlpha = 1;
				ctx.fillStyle = action.id === "run" ? "#f4fbff" : "#d9dde2";
				ctx.font = action.id === "run" ? "600 12px sans-serif" : "600 9px sans-serif";
				const label = typeof action.label === "function" ? action.label(this) : action.label;
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
					hit.run(node, this);
				}
				return Boolean(pressed);
			}
			return false;
		},
	};
}

function installClipboardTextNode(node) {
	const textWidget = getWidget(node, "text");
	const overrideWidget = getWidget(node, "clipboard_override");
	const outputWidget = getWidget(node, "display_text");
	if (!textWidget || !overrideWidget || !outputWidget) {
		return;
	}
	hideWidget(overrideWidget);
	outputWidget.label = "OUTPUT";
	if (outputWidget.inputEl) {
		outputWidget.inputEl.readOnly = true;
		outputWidget.inputEl.style.opacity = 0.75;
	}
	overrideWidget.serializeValue = () => node.jhForceClipboardText
		? JSON.stringify({ force: true, text: node.jhClipboardOverrideText ?? "" })
		: "";
	if (getWidget(node, "jh_text_clipboard_hotkey")) {
		return;
	}

	const pasteText = async () => {
		try {
			const text = await navigator.clipboard.readText();
			node.jhClipboardOverrideText = text ?? "";
			setWidgetValue(node, textWidget, node.jhClipboardOverrideText);
			return true;
		} catch (error) {
			console.error("[JH Text Clipboard] Clipboard read failed:", error);
			toast("warn", "Clipboard", "Clipboard text could not be read. Browser permission may be blocked.");
			return false;
		}
	};

	const originalOnDragOver = node.onDragOver;
	node.onDragOver = function (event) {
		if (hasPromptMediaFileDrop(event)) {
			return true;
		}
		return originalOnDragOver?.apply(this, arguments) ?? false;
	};

	const originalOnDragDrop = node.onDragDrop;
	node.onDragDrop = async function (event) {
		const file = getPromptMediaFileFromDropEvent(event);
		if (!file) {
			return originalOnDragDrop?.apply(this, arguments) ?? false;
		}
		event.preventDefault?.();
		event.stopPropagation?.();
		if (this.jhMetadataDropBusy) {
			return true;
		}
		this.jhMetadataDropBusy = true;
		try {
			const text = await readPromptFromMediaFile(file);
			this.jhClipboardOverrideText = text;
			setWidgetValue(this, textWidget, text);
			toast("success", "JH Text Clipboard", "Prompt loaded from file metadata.");
		} catch (error) {
			console.error("[JH Text Clipboard] Metadata drop failed:", error);
			toast("warn", "JH Text Clipboard", error?.message || "Prompt metadata could not be read.");
		} finally {
			this.jhMetadataDropBusy = false;
		}
		return true;
	};

	node.jhPasteTextAndRun = async () => {
		if (node.jhClipboardBusy) {
			return;
		}
		node.jhClipboardBusy = true;
		try {
			if (await pasteText()) {
				node.jhForceClipboardText = true;
				try {
					await app.queuePrompt(0, 1);
				} finally {
					node.jhForceClipboardText = false;
				}
			}
		} catch (error) {
			console.error("[JH Text Clipboard] Queue failed:", error);
			toast("warn", "JH Text Clipboard", "The text was pasted, but the workflow could not be queued.");
		} finally {
			node.jhClipboardBusy = false;
		}
	};

	node.jhHotkeyWidgetName = "jh_text_clipboard_hotkey";
	node.jhHotkeyAction = node.jhPasteTextAndRun;
	const actionsWidget = node.addCustomWidget(makeTextClipboardActions(pasteText, node.jhPasteTextAndRun));
	const originalOnExecuted = node.onExecuted;
	node.onExecuted = function (message) {
		originalOnExecuted?.apply(this, arguments);
		const text = Array.isArray(message?.text) ? message.text.join("\n") : (message?.text ?? "");
		setWidgetValue(this, outputWidget, text);
	};
	const originalOnConfigure = node.onConfigure;
	node.onConfigure = function (info) {
		const configuredValues = info?.widgets_values;
		originalOnConfigure?.apply(this, arguments);
		if (Array.isArray(configuredValues) && configuredValues.length === 2 && typeof configuredValues[1] === "string") {
			actionsWidget.value = configuredValues[1];
			overrideWidget.value = "";
		} else if (Array.isArray(configuredValues) && configuredValues.length === 3 && typeof configuredValues[2] === "string") {
			actionsWidget.value = configuredValues[2];
			outputWidget.value = "";
		}
		hideWidget(overrideWidget);
		const restoredOutput = info?.properties?.jh_clipboard_output;
		if (typeof restoredOutput === "string") {
			setWidgetValue(this, outputWidget, restoredOutput);
		}
	};
	requestAnimationFrame(() => {
		const size = node.computeSize?.();
		if (size) {
			node.onResize?.([Math.max(node.size[0], size[0]), Math.max(node.size[1], size[1])]);
		}
		app.graph?.setDirtyCanvas?.(true, true);
	});
}

function setFeedWidgetVisible(widget, visible) {
	if (!widget) {
		return;
	}
	if (!widget.jhFeedComputeSizeSaved) {
		widget.jhFeedComputeSizeSaved = true;
		widget.jhFeedComputeSize = widget.computeSize;
	}
	widget.hidden = !visible;
	if (visible) {
		if (widget.jhFeedComputeSize) {
			widget.computeSize = widget.jhFeedComputeSize;
		} else {
			delete widget.computeSize;
		}
	} else {
		widget.computeSize = () => [0, -4];
	}
	if (widget.inputEl) {
		widget.inputEl.style.display = visible ? "" : "none";
	}
}

function installAutoImageFeedNode(node) {
	installAutoFeedPreview(node);
	if (node.jhAutoImageFeedInstalled) {
		requestAnimationFrame(() => node.jhUpdateAutoImageFeedUi?.());
		return;
	}
	const sourceWidget = getWidget(node, "source");
	const queryWidget = getWidget(node, "query");
	if (!sourceWidget || !queryWidget) {
		requestAnimationFrame(() => installAutoImageFeedNode(node));
		return;
	}
	node.jhAutoImageFeedInstalled = true;
	const stopButton = node.addWidget("button", "Stop Auto Image Feed", "Stop Auto Image Feed", async () => {
		const previewWidget = getWidget(node, "jh_auto_feed_preview");
		if (previewWidget) previewWidget.status = "Stop requested...";
		node.setDirtyCanvas?.(true, true);
		try {
			const response = await api.fetchApi("/jh/auto-feed/stop", { method: "POST" });
			const result = await response.json();
			if (!response.ok) throw new Error(result.error || `Stop failed: ${response.status}`);
			toast("info", "JH Auto Image Feed", result.stopped ? "Crawler stopped." : "Stop requested.");
		} catch (error) {
			console.error("[JH Auto Image Feed] Stop failed:", error);
			toast("warn", "JH Auto Image Feed", error.message || "Crawler could not be stopped.");
		}
	}, { serialize: false });
	stopButton.serialize = false;
	const recentPlaceholder = "Select a successful search...";
	const recentWidget = node.addWidget("combo", "recent successful searches", recentPlaceholder, (value) => {
		const preset = node.jhAutoFeedPresetOptions?.get(value);
		if (!preset) return;
		node.jhSelectedAutoFeedPreset = preset;
		setWidgetValue(node, sourceWidget, preset.source);
		requestAnimationFrame(() => {
			for (const [name, presetValue] of Object.entries(preset.values || {})) {
				setWidgetValue(node, getWidget(node, name), presetValue);
			}
			node.jhUpdateAutoImageFeedUi?.();
		});
	}, { values: [recentPlaceholder], serialize: false });
	recentWidget.serialize = false;
	const deleteRecentButton = node.addWidget("button", "Delete Selected Search", "Delete Selected Search", async () => {
		const preset = node.jhSelectedAutoFeedPreset;
		if (!preset) {
			toast("info", "JH Auto Image Feed", "Select a successful search first.");
			return;
		}
		try {
			const response = await api.fetchApi("/jh/auto-feed/presets/delete", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ id: preset.id }),
			});
			const result = await response.json();
			if (!response.ok) throw new Error(result.error || `Delete failed: ${response.status}`);
			node.jhSelectedAutoFeedPreset = null;
			await node.jhRefreshAutoFeedPresets?.();
		} catch (error) {
			console.error("[JH Auto Image Feed] Successful search delete failed:", error);
			toast("warn", "JH Auto Image Feed", error.message || "Search could not be deleted.");
		}
	}, { serialize: false });
	deleteRecentButton.serialize = false;
	const updateRecentOptions = () => {
		const presets = (node.jhAutoFeedPresets || []).filter((preset) => preset.source === sourceWidget.value);
		const options = [recentPlaceholder, ...presets.map((preset) => preset.label)];
		node.jhAutoFeedPresetOptions = new Map(presets.map((preset) => [preset.label, preset]));
		recentWidget.options.values = options;
		if (!options.includes(recentWidget.value)) {
			recentWidget.value = recentPlaceholder;
			node.jhSelectedAutoFeedPreset = null;
		}
		node.setDirtyCanvas?.(true, true);
	};
	node.jhRefreshAutoFeedPresets = async () => {
		try {
			const response = await api.fetchApi("/jh/auto-feed/presets");
			const result = await response.json();
			if (!response.ok) throw new Error(result.error || `Load failed: ${response.status}`);
			node.jhAutoFeedPresets = Array.isArray(result.presets) ? result.presets : [];
			updateRecentOptions();
		} catch (error) {
			console.error("[JH Auto Image Feed] Successful searches could not be loaded:", error);
		}
	};

	const sourceUi = {
		"Google Images": { label: "search keywords", placeholder: "portrait photography woman", show: ["period", "safe_search", "scroll_rounds"] },
		"Instagram User": { label: "Instagram username", placeholder: "natgeo", show: ["scroll_rounds", "media_mode", "video_scan_fps", "video_max_seconds", "quality_filter", "min_popularity", "min_comments"] },
		"Instagram Hashtag": { label: "Instagram hashtag", placeholder: "portraitphotography", show: ["scroll_rounds", "media_mode", "video_scan_fps", "video_max_seconds", "quality_filter", "min_popularity", "min_comments"] },
		"Reddit Subreddit": { showQuery: false, show: ["reddit_mode", "period", "scroll_rounds", "media_mode", "video_scan_fps", "video_max_seconds", "quality_filter", "min_popularity", "min_comments"] },
		"DCInside Gallery": { showQuery: false, show: ["dc_gallery", "title_filter", "dc_random_mode", "quality_filter", "min_popularity", "min_comments", "min_views"] },
		"Arca.live Channel": { showQuery: false, show: ["arca_channel", "arca_mode", "title_filter", "quality_filter", "min_popularity", "min_comments"] },
		"Website URL": { label: "website URL", placeholder: "https://example.com/gallery", show: ["scroll_rounds", "url_single_character_sheet"] },
		"X Search": { label: "X search keywords", placeholder: "portrait photography woman", show: ["search_mode", "scroll_rounds", "media_mode", "video_scan_fps", "video_max_seconds", "quality_filter", "min_popularity", "min_comments"] },
		"Mixed Sources": {
			label: "source list",
			placeholder: "google | portrait woman\narca | aireal\nurl | https://example.com/gallery",
			show: ["period", "safe_search", "scroll_rounds", "quality_filter", "min_popularity", "min_comments", "min_views"],
		},
	};
	const conditionalWidgets = ["period", "safe_search", "scroll_rounds", "dc_gallery", "dc_gallery_custom", "title_filter", "dc_random_mode", "arca_channel", "arca_mode", "reddit_mode", "reddit_subreddit", "reddit_keyword", "search_mode", "media_mode", "video_scan_fps", "video_max_seconds", "quality_filter", "min_popularity", "min_comments", "min_views", "url_single_character_sheet"];
	const rememberedWidgetNames = ["query", "max_candidates", ...conditionalWidgets];
	const widgetDefaults = Object.fromEntries(rememberedWidgetNames.map((name) => {
		const widget = getWidget(node, name);
		return [name, widget?.options?.default ?? widget?.options?.values?.[0] ?? widget?.value];
	}));
	let activeSource = sourceWidget.value;
	const saveSourceValues = (source) => {
		if (!source) return;
		node.properties ||= {};
		node.properties.jh_auto_feed_source_values ||= {};
		node.properties.jh_auto_feed_source_values[source] = Object.fromEntries(rememberedWidgetNames.map((name) => {
			const widget = getWidget(node, name);
			return [name, widget?.value];
		}));
	};
	const restoreSourceValues = (source) => {
		const values = node.properties?.jh_auto_feed_source_values?.[source] || widgetDefaults;
		for (const name of rememberedWidgetNames) {
			const widget = getWidget(node, name);
			if (widget && values[name] !== undefined) widget.value = values[name];
		}
	};

	const updateSourceUi = () => {
		const dcGalleryWidget = getWidget(node, "dc_gallery");
		const dcCustomWidget = getWidget(node, "dc_gallery_custom");
		const titleFilterWidget = getWidget(node, "title_filter");
		const maxCandidatesWidget = getWidget(node, "max_candidates");
		const dcRandomModeWidget = getWidget(node, "dc_random_mode");
		const arcaChannelWidget = getWidget(node, "arca_channel");
		const arcaModeWidget = getWidget(node, "arca_mode");
		const redditModeWidget = getWidget(node, "reddit_mode");
		const redditSubredditWidget = getWidget(node, "reddit_subreddit");
		const redditKeywordWidget = getWidget(node, "reddit_keyword");
		const mediaModeWidget = getWidget(node, "media_mode");
		const videoScanFpsWidget = getWidget(node, "video_scan_fps");
		const videoMaxSecondsWidget = getWidget(node, "video_max_seconds");
		const qualityFilterWidget = getWidget(node, "quality_filter");
		const minPopularityWidget = getWidget(node, "min_popularity");
		const minCommentsWidget = getWidget(node, "min_comments");
		const minViewsWidget = getWidget(node, "min_views");
		const minMegapixelsWidget = getWidget(node, "min_megapixels");
		const faceCheckWidget = getWidget(node, "face_check");
		const faceConfidenceWidget = getWidget(node, "face_confidence");
		const faceModelWidget = getWidget(node, "face_model");
		if (redditModeWidget && !["Subreddit", "Keyword Search"].includes(redditModeWidget.value)) {
			redditModeWidget.value = "Subreddit";
		}
		if (mediaModeWidget && !["Images + Video/GIF", "Images Only"].includes(mediaModeWidget.value)) {
			mediaModeWidget.value = "Images + Video/GIF";
		}
		if (arcaModeWidget && !["Best", "All"].includes(arcaModeWidget.value)) {
			arcaModeWidget.value = "Best";
		}
		if (videoScanFpsWidget && (typeof videoScanFpsWidget.value !== "number" || videoScanFpsWidget.value < 0.25 || videoScanFpsWidget.value > 10)) {
			videoScanFpsWidget.value = 2.0;
		}
		if (videoMaxSecondsWidget && (typeof videoMaxSecondsWidget.value !== "number" || videoMaxSecondsWidget.value < 1 || videoMaxSecondsWidget.value > 300)) {
			videoMaxSecondsWidget.value = 30;
		}
		if (dcGalleryWidget) {
			dcGalleryWidget.label = "DC gallery";
		}
		if (dcCustomWidget) {
			dcCustomWidget.label = "gallery ID or URL";
		}
		if (titleFilterWidget) {
			titleFilterWidget.label = "title contains (or re:regex)";
		}
		if (maxCandidatesWidget) {
			maxCandidatesWidget.label = sourceWidget.value === "Arca.live Channel"
				? "posts per page (0 = search until found)"
				: "max candidates (0 = search until found)";
		}
		const urlSingleCharacterSheetWidget = getWidget(node, "url_single_character_sheet");
		if (urlSingleCharacterSheetWidget) {
			urlSingleCharacterSheetWidget.label = "URL character sheets";
		}
		if (dcRandomModeWidget) {
			dcRandomModeWidget.label = "DC random behavior";
		}
		if (arcaChannelWidget) {
			arcaChannelWidget.label = "Arca.live channel";
		}
		if (arcaModeWidget) {
			arcaModeWidget.label = "Arca.live board mode";
		}
		if (redditModeWidget) {
			redditModeWidget.label = "Reddit mode";
		}
		if (redditSubredditWidget) {
			redditSubredditWidget.label = "subreddit (r/name or name)";
		}
		if (redditKeywordWidget) {
			redditKeywordWidget.label = "search keywords";
		}
		if (mediaModeWidget) {
			mediaModeWidget.label = "media type";
		}
		if (videoScanFpsWidget) {
			videoScanFpsWidget.label = "video scan FPS";
		}
		if (videoMaxSecondsWidget) {
			videoMaxSecondsWidget.label = "video scan seconds";
		}
		if (qualityFilterWidget) {
			qualityFilterWidget.label = "filter low-quality posts";
		}
		if (minPopularityWidget) {
			minPopularityWidget.label = sourceWidget.value.startsWith("Instagram") || sourceWidget.value === "X Search" ? "minimum likes" : sourceWidget.value === "DCInside Gallery" || sourceWidget.value === "Arca.live Channel" ? "minimum recommends" : sourceWidget.value === "Reddit Subreddit" ? "minimum post score" : "minimum popularity";
		}
		if (minCommentsWidget) {
			minCommentsWidget.label = sourceWidget.value === "X Search" ? "minimum replies" : "minimum comments";
		}
		if (minViewsWidget) {
			minViewsWidget.label = "minimum views";
		}
		if (minMegapixelsWidget) {
			minMegapixelsWidget.label = "minimum megapixels (0 = off)";
		}
		if (faceCheckWidget) {
			faceCheckWidget.label = "require detected face";
		}
		if (faceConfidenceWidget) {
			faceConfidenceWidget.label = "minimum face confidence";
		}
		if (faceModelWidget) faceModelWidget.label = "face detector";
		const config = sourceUi[sourceWidget.value] || sourceUi["Google Images"];
		setFeedWidgetVisible(queryWidget, config.showQuery !== false);
		if (config.label) {
			queryWidget.label = config.label;
		}
		if (queryWidget.inputEl && config.placeholder) {
			queryWidget.inputEl.placeholder = config.placeholder;
		}
		for (const name of conditionalWidgets) {
			setFeedWidgetVisible(getWidget(node, name), config.show.includes(name));
		}
		setFeedWidgetVisible(maxCandidatesWidget, sourceWidget.value !== "Website URL");
		if (sourceWidget.value === "DCInside Gallery") {
			setFeedWidgetVisible(dcCustomWidget, dcGalleryWidget?.value === "직접 입력 (ID/URL)");
		}
		if (sourceWidget.value === "Reddit Subreddit") {
			setFeedWidgetVisible(redditSubredditWidget, redditModeWidget?.value !== "Keyword Search");
			setFeedWidgetVisible(redditKeywordWidget, redditModeWidget?.value === "Keyword Search");
		}
		if (config.show.includes("media_mode") && mediaModeWidget?.value === "Images Only") {
			setFeedWidgetVisible(videoScanFpsWidget, false);
			setFeedWidgetVisible(videoMaxSecondsWidget, false);
		}
		if (!qualityFilterWidget?.value) {
			for (const name of ["min_popularity", "min_comments", "min_views"]) setFeedWidgetVisible(getWidget(node, name), false);
		}
		setFeedWidgetVisible(faceConfidenceWidget, Boolean(faceCheckWidget?.value));
		setFeedWidgetVisible(faceModelWidget, Boolean(faceCheckWidget?.value));
		requestAnimationFrame(() => {
			const computed = node.computeSize?.();
			if (computed) {
				node.setSize?.([Math.max(node.size[0], computed[0]), computed[1]]);
			}
			node.setDirtyCanvas?.(true, true);
			app.graph?.setDirtyCanvas?.(true, true);
		});
	};
	node.jhUpdateAutoImageFeedUi = updateSourceUi;

	const sourceCallback = sourceWidget.callback;
	sourceWidget.callback = function(value) {
		saveSourceValues(activeSource);
		sourceCallback?.apply(this, arguments);
		activeSource = value ?? sourceWidget.value;
		restoreSourceValues(activeSource);
		updateSourceUi();
		updateRecentOptions();
	};
	const installDcGalleryCallback = () => {
		const dcGalleryWidget = getWidget(node, "dc_gallery");
		if (!dcGalleryWidget || dcGalleryWidget.jhAutoImageFeedInstalled) {
			return;
		}
		dcGalleryWidget.jhAutoImageFeedInstalled = true;
		const dcGalleryCallback = dcGalleryWidget.callback;
		dcGalleryWidget.callback = function(value) {
			dcGalleryCallback?.apply(this, arguments);
			updateSourceUi();
		};
	};
	const installRedditModeCallback = () => {
		const redditModeWidget = getWidget(node, "reddit_mode");
		if (!redditModeWidget || redditModeWidget.jhAutoImageFeedInstalled) {
			return;
		}
		redditModeWidget.jhAutoImageFeedInstalled = true;
		const redditModeCallback = redditModeWidget.callback;
		redditModeWidget.callback = function(value) {
			redditModeCallback?.apply(this, arguments);
			updateSourceUi();
		};
	};
	const installMediaModeCallback = () => {
		const mediaModeWidget = getWidget(node, "media_mode");
		if (!mediaModeWidget || mediaModeWidget.jhAutoImageFeedInstalled) {
			return;
		}
		mediaModeWidget.jhAutoImageFeedInstalled = true;
		const mediaModeCallback = mediaModeWidget.callback;
		mediaModeWidget.callback = function(value) {
			mediaModeCallback?.apply(this, arguments);
			updateSourceUi();
		};
	};
	const installQualityFilterCallback = () => {
		const qualityFilterWidget = getWidget(node, "quality_filter");
		if (!qualityFilterWidget || qualityFilterWidget.jhAutoImageFeedInstalled) return;
		qualityFilterWidget.jhAutoImageFeedInstalled = true;
		const qualityFilterCallback = qualityFilterWidget.callback;
		qualityFilterWidget.callback = function(value) {
			qualityFilterCallback?.apply(this, arguments);
			updateSourceUi();
		};
	};
	const installFaceCheckCallback = () => {
		const faceCheckWidget = getWidget(node, "face_check");
		if (!faceCheckWidget || faceCheckWidget.jhAutoImageFeedInstalled) return;
		faceCheckWidget.jhAutoImageFeedInstalled = true;
		const faceCheckCallback = faceCheckWidget.callback;
		faceCheckWidget.callback = function(value) {
			faceCheckCallback?.apply(this, arguments);
			updateSourceUi();
		};
	};
	installDcGalleryCallback();
	installRedditModeCallback();
	installMediaModeCallback();
	installQualityFilterCallback();
	installFaceCheckCallback();
	updateSourceUi();
	node.jhRefreshAutoFeedPresets();
	requestAnimationFrame(() => requestAnimationFrame(() => {
		installDcGalleryCallback();
		installRedditModeCallback();
		installMediaModeCallback();
		installQualityFilterCallback();
		installFaceCheckCallback();
		updateSourceUi();
	}));
}

app.registerExtension({
	name: "jh.text.clipboard.v7",
	loadedGraphNode(node) {
		if (node.comfyClass === "JHAutoImageFeed") {
			installAutoImageFeedNode(node);
			return;
		}
		if (node.comfyClass === "JHPriorityPassthrough") {
			installPriorityPassthroughNode(node);
			return;
		}
		migrateSavedPickerNode(node);
		if (["JHShowText", "JHLlamaPrompt"].includes(node.comfyClass)) {
			installShowTextNode(node);
			if (node.comfyClass === "JHLlamaPrompt") {
				installLlamaImagePreview(node);
			}
		}
	},
	async nodeCreated(node) {
		if (node.comfyClass === "JHAutoImageFeed") {
			installAutoImageFeedNode(node);
			return;
		}
		if (node.comfyClass === "JHPriorityPassthrough") {
			installPriorityPassthroughNode(node);
			return;
		}
		if (node.comfyClass === "JHClipboardImage") {
			installClipboardImageNode(node);
			return;
		}
		if (node.comfyClass === "JHImagePreview") {
			installImagePreviewNode(node);
			return;
		}

		if (["JHShowText", "JHLlamaPrompt"].includes(node.comfyClass)) {
			installShowTextNode(node);
			if (node.comfyClass === "JHLlamaPrompt") {
				installLlamaImagePreview(node);
			}
			return;
		}
		if (node.comfyClass === "JHLoraLoader") {
			await installLoraStackNode(node);
			return;
		}
		if (["JHSavedPicker", "JHSavedTextPicker", "JHSavedTextLoraPicker"].includes(node.comfyClass)) {
			migrateSavedPickerNode(node);
			await installLoraStackNode(node);
			installSavedTextPickerNode(node);
			return;
		}

		if (["JHClipboardText", "FluxClipboardText"].includes(node.comfyClass)) {
			installClipboardTextNode(node);
		}
	},
});
