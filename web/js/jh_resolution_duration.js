import { app } from "../../../scripts/app.js";

function updateAspectRatioState(node) {
    const useImage = node.widgets?.find((widget) => widget.name === "use_image_resolution");
    const aspectRatio = node.widgets?.find((widget) => widget.name === "aspect_ratio");
    if (!useImage || !aspectRatio) return;

    aspectRatio.disabled = Boolean(useImage.value);
    node.setDirtyCanvas?.(true, true);
}

app.registerExtension({
    name: "JH.ResolutionDuration",
    nodeCreated(node) {
        if (node.comfyClass !== "ResolutionDurationCalculator") return;

        const useImage = node.widgets?.find((widget) => widget.name === "use_image_resolution");
        if (!useImage) return;

        const originalCallback = useImage.callback;
        useImage.callback = function (value, ...args) {
            const result = originalCallback?.call(this, value, ...args);
            updateAspectRatioState(node);
            return result;
        };
        updateAspectRatioState(node);
        setTimeout(() => updateAspectRatioState(node), 0);
    },
});
