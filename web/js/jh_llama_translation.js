import { app } from "../../../scripts/app.js";

function getWidget(node, name) {
	return node.widgets?.find((widget) => widget.name === name);
}

function hideWidget(widget) {
	if (!widget) return;
	widget.hidden = true;
	widget.computeSize = () => [0, -4];
}

function setToggle(node, widget, value) {
	widget.value = value;
	widget.callback?.(value);
	node.setDirtyCanvas?.(true, true);
	app.graph?.setDirtyCanvas?.(true, true);
}

function makeTranslationControls(states) {
	const controls = [
		{ label: "Prompt EN", widget: states.prompt },
		{ label: "Instruction EN", widget: states.instruction },
		{ label: "Supplement EN", widget: states.supplemental },
	];
	return {
		type: "jh_llama_translation_controls",
		name: "jh_llama_translation_controls",
		serialize: false,
		bounds: [],
		computeSize(width) {
			return [width || 0, 30];
		},
		draw(ctx, node, width, y) {
			const margin = 12;
			const gap = 5;
			const buttonWidth = (width - margin * 2 - gap * 2) / 3;
			this.bounds = controls.map((control, index) => [margin + index * (buttonWidth + gap), y + 2, buttonWidth, 24]);
			ctx.save();
			ctx.font = "600 10px sans-serif";
			ctx.textAlign = "center";
			ctx.textBaseline = "middle";
			for (let index = 0; index < controls.length; index++) {
				const control = controls[index];
				const [x, top, itemWidth, height] = this.bounds[index];
				ctx.fillStyle = control.widget.value ? "#356f58" : "#333";
				ctx.strokeStyle = control.widget.value ? "#7ed6a5" : "#666";
				ctx.beginPath();
				ctx.roundRect(x, top, itemWidth, height, 6);
				ctx.fill();
				ctx.stroke();
				ctx.fillStyle = control.widget.value ? "#e4fff0" : "#aaa";
				ctx.fillText(control.label, x + itemWidth / 2, top + height / 2);
			}
			ctx.restore();
		},
		mouse(event, pos, node) {
			if (event.type !== "pointerdown") return false;
			const index = this.bounds.findIndex(([x, y, width, height]) => pos[0] >= x && pos[0] <= x + width && pos[1] >= y && pos[1] <= y + height);
			if (index < 0) return false;
			const widget = controls[index].widget;
			setToggle(node, widget, !widget.value);
			return true;
		},
	};
}

function installLlamaTranslation(node) {
	if (node.jhLlamaTranslationInstalled) return;
	const states = {
		prompt: getWidget(node, "translate_prompt"),
		instruction: getWidget(node, "translate_instruction"),
		supplemental: getWidget(node, "translate_supplemental"),
	};
	if (!states.prompt || !states.instruction || !states.supplemental) return;
	node.jhLlamaTranslationInstalled = true;
	for (const widget of Object.values(states)) hideWidget(widget);
	node.addCustomWidget(makeTranslationControls(states));
	const originalOnConfigure = node.onConfigure;
	node.onConfigure = function () {
		originalOnConfigure?.apply(this, arguments);
		for (const name of ["translate_prompt", "translate_instruction", "translate_supplemental"]) hideWidget(getWidget(this, name));
	};
	requestAnimationFrame(() => {
		const size = node.computeSize?.();
		if (size) node.onResize?.([Math.max(node.size[0], size[0]), size[1]]);
		node.setDirtyCanvas?.(true, true);
	});
}

app.registerExtension({
	name: "jh.llama.translation",
	loadedGraphNode(node) {
		if (node.comfyClass === "JHLlamaPrompt") installLlamaTranslation(node);
	},
	nodeCreated(node) {
		if (node.comfyClass === "JHLlamaPrompt") installLlamaTranslation(node);
	},
});
