import { api } from "../../../scripts/api.js";
import { app } from "../../../scripts/app.js";

const WIDGET_NAME = "jh_subgraph_sampling_preview";

function getNode(graph, id) {
    return graph?.getNodeById?.(id) ?? graph?.getNodeById?.(Number(id));
}

function exposesPreviewFrom(node, sourceNodeId) {
    const exposures = node.properties?.previewExposures;
    return Array.isArray(exposures) && exposures.some((exposure) =>
        String(exposure.sourceNodeId) === String(sourceNodeId) &&
        String(exposure.sourcePreviewName).startsWith("$$canvas-image-preview")
    );
}

function makePreviewWidget() {
    return {
        type: WIDGET_NAME,
        name: WIDGET_NAME,
        serialize: false,
        image: null,
        previewToken: 0,
        computeSize(width) {
            return [width || 0, 250];
        },
        draw(ctx, node, width, y) {
            if (!this.image) return;

            const left = 10;
            const drawWidth = Math.max(1, width - 20);
            const drawHeight = 238;
            const scale = Math.min(drawWidth / this.image.width, drawHeight / this.image.height);
            const imageWidth = this.image.width * scale;
            const imageHeight = this.image.height * scale;

            ctx.save();
            ctx.fillStyle = "#111";
            ctx.fillRect(left, y, drawWidth, drawHeight);
            ctx.drawImage(
                this.image,
                left + (drawWidth - imageWidth) / 2,
                y + (drawHeight - imageHeight) / 2,
                imageWidth,
                imageHeight,
            );
            ctx.restore();
        },
    };
}

function getPreviewWidget(node) {
    let widget = node.widgets?.find((item) => item.name === WIDGET_NAME);
    if (widget) return widget;

    widget = node.addCustomWidget(makePreviewWidget());
    const size = node.computeSize?.();
    if (size) {
        node.setSize?.([
            Math.max(node.size[0], size[0]),
            Math.max(node.size[1], size[1]),
        ]);
    }
    return widget;
}

function updatePreview(node, blob) {
    const widget = getPreviewWidget(node);
    const token = widget.previewToken + 1;
    widget.previewToken = token;

    const url = URL.createObjectURL(blob);
    const image = new Image();
    image.onload = () => {
        URL.revokeObjectURL(url);
        if (widget.previewToken !== token) return;
        widget.image = image;
        node.setDirtyCanvas?.(true, true);
    };
    image.onerror = () => URL.revokeObjectURL(url);
    image.src = url;
}

function updateSubgraphPreviews(displayNodeId, blob) {
    const parts = String(displayNodeId ?? "").split(":");
    if (!blob || parts.length < 2) return;

    let graph = app.rootGraph;
    for (let index = 0; index < parts.length - 1; index += 1) {
        const host = getNode(graph, parts[index]);
        if (!host?.subgraph) return;
        if (exposesPreviewFrom(host, parts[index + 1])) {
            updatePreview(host, blob);
        }
        graph = host.subgraph;
    }
}

let executingNodeId = null;
let lastMetadataPreview = 0;

api.addEventListener("executing", ({ detail }) => {
    executingNodeId = detail ? String(detail) : null;
});

api.addEventListener("b_preview_with_metadata", ({ detail }) => {
    lastMetadataPreview = performance.now();
    updateSubgraphPreviews(detail?.displayNodeId, detail?.blob);
});

api.addEventListener("b_preview", ({ detail }) => {
    if (performance.now() - lastMetadataPreview < 50) return;
    updateSubgraphPreviews(executingNodeId, detail);
});

app.registerExtension({
    name: "JH.SubgraphSamplingPreview",
});
