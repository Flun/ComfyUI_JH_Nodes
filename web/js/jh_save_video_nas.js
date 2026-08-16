import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

function installVideoPreview(node) {
    if (node.jhNasVideoPreviewInstalled) return;
    node.jhNasVideoPreviewInstalled = true;
    let previewVisible = false;

    const panel = document.createElement("div");
    panel.classList.add("comfy-img-preview");
    panel.style.width = "100%";
    panel.style.height = "100%";
    panel.style.minHeight = "0";
    panel.style.boxSizing = "border-box";
    panel.style.overflow = "hidden";
    panel.style.display = "none";

    const video = document.createElement("video");
    video.controls = true;
    video.loop = true;
    video.playsInline = true;
    video.preload = "metadata";
    video.style.width = "100%";
    video.style.height = "100%";
    video.style.objectFit = "contain";
    video.style.borderRadius = "6px";

    const image = document.createElement("img");
    image.style.width = "100%";
    image.style.height = "100%";
    image.style.objectFit = "contain";
    image.style.borderRadius = "6px";
    image.style.display = "none";

    panel.append(video, image);
    node.addDOMWidget("jh_nas_video_preview", "preview", panel, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => previewVisible ? 180 : 0,
    });

    const originalOnExecuted = node.onExecuted;
    node.onExecuted = function (message) {
        originalOnExecuted?.apply(this, arguments);
        const preview = message?.jh_video_preview?.[0];
        if (!preview?.token) return;

        const src = api.apiURL(`/jh/nas-preview?token=${encodeURIComponent(preview.token)}&v=${Date.now()}`);
        const isStillImageContainer = preview.format === "gif" || preview.format === "webp";
        video.pause();
        video.removeAttribute("src");
        image.removeAttribute("src");
        video.style.display = isStillImageContainer ? "none" : "block";
        image.style.display = isStillImageContainer ? "block" : "none";
        if (isStillImageContainer) {
            image.src = src;
        } else {
            video.src = src;
            video.load();
        }
        previewVisible = true;
        panel.style.display = "block";
        requestAnimationFrame(() => {
            const minimumSize = this.computeSize();
            this.setSize?.([
                Math.max(this.size[0], 320),
                Math.max(this.size[1], minimumSize[1]),
            ]);
            this.onResize?.(this.size);
            this.setDirtyCanvas?.(true, true);
        });
    };

    const originalOnRemoved = node.onRemoved;
    node.onRemoved = function () {
        video.pause();
        video.removeAttribute("src");
        image.removeAttribute("src");
        originalOnRemoved?.apply(this, arguments);
    };
}

function installImagePreview(node) {
    if (node.jhNasImagePreviewInstalled) return;
    node.jhNasImagePreviewInstalled = true;
    let previewVisible = false;

    const panel = document.createElement("div");
    panel.classList.add("comfy-img-preview");
    Object.assign(panel.style, {
        width: "100%",
        height: "100%",
        minHeight: "0",
        boxSizing: "border-box",
        overflow: "auto",
        display: "none",
        gap: "6px",
        gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
        alignItems: "center",
    });

    node.addDOMWidget("jh_nas_image_preview", "preview", panel, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => previewVisible ? 180 : 0,
    });

    const originalOnExecuted = node.onExecuted;
    node.onExecuted = function (message) {
        originalOnExecuted?.apply(this, arguments);
        const previews = message?.jh_image_preview;
        if (!Array.isArray(previews) || !previews.length) return;

        const images = previews.filter((preview) => preview?.token).map((preview) => {
            const image = document.createElement("img");
            image.src = api.apiURL(`/jh/nas-preview?token=${encodeURIComponent(preview.token)}&v=${Date.now()}`);
            image.alt = preview.filename || "Saved image";
            Object.assign(image.style, {
                width: "100%",
                height: "100%",
                minHeight: "0",
                objectFit: "contain",
                borderRadius: "6px",
            });
            return image;
        });
        if (!images.length) return;

        panel.replaceChildren(...images);
        previewVisible = true;
        panel.style.display = "grid";
        requestAnimationFrame(() => {
            const minimumSize = this.computeSize();
            this.setSize?.([
                Math.max(this.size[0], 320),
                Math.max(this.size[1], minimumSize[1]),
            ]);
            this.onResize?.(this.size);
            this.setDirtyCanvas?.(true, true);
        });
    };

    const originalOnRemoved = node.onRemoved;
    node.onRemoved = function () {
        panel.replaceChildren();
        originalOnRemoved?.apply(this, arguments);
    };
}

app.registerExtension({
    name: "JH.SaveNASPreview",
    nodeCreated(node) {
        if (node.comfyClass === "SaveVideoToNAS") {
            installVideoPreview(node);
        } else if (node.comfyClass === "SaveImageToNAS") {
            installImagePreview(node);
        }
    },
});
