import { app } from "../../../scripts/app.js";

function getWidget(node, name) {
	return node.widgets?.find((widget) => widget.name === name);
}

function hideWidget(widget) {
	if (!widget) return;
	widget.hidden = true;
	widget.computeSize = () => [0, -4];
}

function setStoredValue(node, widget, value) {
	widget.value = value;
	widget.callback?.(value);
	node.setDirtyCanvas?.(true, true);
	app.graph?.setDirtyCanvas?.(true, true);
}

function detachWidget(node, widget) {
	const index = node.widgets?.indexOf(widget) ?? -1;
	if (index < 0) return false;
	node.widgets.splice(index, 1);
	widget.onRemove?.();
	return true;
}

function parseSlots(value) {
	try {
		const slots = JSON.parse(value || "[]");
		return Array.isArray(slots) ? slots : [];
	} catch {
		return [];
	}
}

function fitText(ctx, text, maxWidth) {
	if (ctx.measureText(text).width <= maxWidth) return text;
	let shortened = text;
	while (shortened.length > 1 && ctx.measureText(`${shortened}...`).width > maxWidth) {
		shortened = shortened.slice(0, -1);
	}
	return `${shortened}...`;
}

function slotLabel(value) {
	const title = value.title.trim();
	if (title) return title;
	return value.prompt.trim().replace(/\s+/g, " ") || "Empty prompt";
}

function showSlotEditor(slot, onChange) {
	const overlay = document.createElement("div");
	overlay.style.cssText = "position:fixed;inset:0;z-index:100000;background:#0009;display:flex;align-items:center;justify-content:center";
	const panel = document.createElement("div");
	panel.style.cssText = "width:min(680px,88vw);background:#252525;border:1px solid #666;border-radius:10px;padding:16px;display:flex;flex-direction:column;gap:8px;box-shadow:0 16px 48px #000;color:#ddd";
	const titleLabel = document.createElement("label");
	titleLabel.textContent = "Title (optional)";
	const title = document.createElement("input");
	title.value = slot.value.title;
	title.placeholder = "Name shown on the node";
	title.style.cssText = "padding:9px;background:#111;color:#eee;border:1px solid #666;border-radius:6px";
	const promptLabel = document.createElement("label");
	promptLabel.textContent = "Prompt";
	const prompt = document.createElement("textarea");
	prompt.value = slot.value.prompt;
	prompt.placeholder = "Enter the prompt for this slot";
	prompt.style.cssText = "min-height:220px;resize:vertical;padding:9px;background:#111;color:#eee;border:1px solid #666;border-radius:6px;font:13px sans-serif";
	const done = document.createElement("button");
	done.textContent = "Done";
	done.style.cssText = "align-self:flex-end;padding:8px 24px;background:#506685;color:#fff;border:0;border-radius:6px;cursor:pointer";
	let closed = false;
	const close = () => {
		if (closed) return;
		closed = true;
		slot.value.title = title.value;
		slot.value.prompt = prompt.value;
		onChange();
		overlay.remove();
	};
	done.onclick = close;
	overlay.addEventListener("pointerdown", (event) => {
		if (event.target === overlay) close();
	});
	overlay.addEventListener("keydown", (event) => {
		if (event.key === "Escape") close();
	});
	panel.append(titleLabel, title, promptLabel, prompt, done);
	overlay.append(panel);
	document.body.append(overlay);
	requestAnimationFrame(() => (slot.value.title ? title : prompt).focus());
}

function contains(bounds, pos) {
	return bounds && pos[0] >= bounds[0] && pos[0] <= bounds[0] + bounds[2] && pos[1] >= bounds[1] && pos[1] <= bounds[1] + bounds[3];
}

function makePromptRow(data, callbacks) {
	return {
		type: "jh_prompt_row",
		name: `jh_prompt_row_${Math.random().toString(36).slice(2)}`,
		value: {
			id: typeof data.id === "string" ? data.id : Math.random().toString(36).slice(2),
			enabled: data.enabled !== false,
			translate: data.translate === true,
			title: typeof data.title === "string" ? data.title : "",
			prompt: typeof data.prompt === "string" ? data.prompt : "",
			strength: Number.isFinite(Number(data.strength)) ? Number(data.strength) : 1,
		},
		serialize: false,
		bounds: {},
		computeSize(width) {
			return [width || 0, 28];
		},
		draw(ctx, node, width, y) {
			const x = 12;
			const rowWidth = width - 24;
			this.bounds.row = [x, y, rowWidth, 24];
			this.bounds.toggle = [x + 6, y + 6, 12, 12];
			this.bounds.up = [x + rowWidth - 116, y + 2, 20, 20];
			this.bounds.down = [x + rowWidth - 94, y + 2, 20, 20];
			this.bounds.translate = [x + rowWidth - 142, y + 2, 22, 20];
			this.bounds.reset = [x + rowWidth - 166, y + 2, 22, 20];
			this.bounds.dec = [x + rowWidth - 70, y + 2, 18, 20];
			this.bounds.strength = [x + rowWidth - 52, y + 2, 34, 20];
			this.bounds.inc = [x + rowWidth - 18, y + 2, 18, 20];
			this.bounds.label = [x + 24, y, rowWidth - 194, 24];

			ctx.save();
			ctx.fillStyle = "#202020";
			ctx.strokeStyle = "#666";
			ctx.beginPath();
			ctx.roundRect(x, y, rowWidth, 24, 7);
			ctx.fill();
			ctx.stroke();
			ctx.fillStyle = this.value.enabled ? "#91a9d0" : "#666";
			ctx.beginPath();
			ctx.arc(x + 12, y + 12, 6, 0, Math.PI * 2);
			ctx.fill();
			ctx.globalAlpha = this.value.enabled ? 1 : 0.45;
			ctx.fillStyle = "#ddd";
			ctx.font = "12px sans-serif";
			ctx.textBaseline = "middle";
			ctx.textAlign = "left";
			ctx.fillText(fitText(ctx, slotLabel(this.value), this.bounds.label[2]), this.bounds.label[0], y + 12);
			ctx.textAlign = "center";
			ctx.fillStyle = "#aaa";
			ctx.font = "13px sans-serif";
			ctx.fillText("\u21ba", this.bounds.reset[0] + 11, y + 12);
			ctx.fillStyle = this.value.translate ? "#7ed6a5" : "#777";
			ctx.font = "600 9px sans-serif";
			ctx.fillText("EN", this.bounds.translate[0] + 11, y + 12);
			ctx.fillStyle = "#ddd";
			ctx.font = "12px sans-serif";
			ctx.fillText("\u2191", this.bounds.up[0] + 10, y + 12);
			ctx.fillText("\u2193", this.bounds.down[0] + 10, y + 12);
			ctx.fillText("-", this.bounds.dec[0] + 9, y + 12);
			ctx.font = "10px sans-serif";
			ctx.fillText(this.value.strength.toFixed(2), this.bounds.strength[0] + 17, y + 12);
			ctx.font = "12px sans-serif";
			ctx.fillText("+", this.bounds.inc[0] + 9, y + 12);
			ctx.restore();
		},
		mouse(event, pos, node) {
			if (event.type !== "pointerdown" || !contains(this.bounds.row, pos)) return false;
			if (event.button === 2) {
				new LiteGraph.ContextMenu([{ content: "Remove Prompt", callback: () => callbacks.remove(this) }], { title: "Prompt", event });
				return true;
			}
			if (contains(this.bounds.toggle, pos)) {
				this.value.enabled = !this.value.enabled;
			} else if (contains(this.bounds.reset, pos)) {
				this.value.translate = false;
				this.value.strength = 1;
			} else if (contains(this.bounds.translate, pos)) {
				this.value.translate = !this.value.translate;
			} else if (contains(this.bounds.up, pos)) {
				callbacks.move(this, -1);
				return true;
			} else if (contains(this.bounds.down, pos)) {
				callbacks.move(this, 1);
				return true;
			} else if (contains(this.bounds.dec, pos)) {
				this.value.strength = Math.round((this.value.strength - 0.05) * 100) / 100;
			} else if (contains(this.bounds.inc, pos)) {
				this.value.strength = Math.round((this.value.strength + 0.05) * 100) / 100;
			} else if (contains(this.bounds.strength, pos)) {
				app.canvas.prompt("Prompt strength", this.value.strength, (value) => {
					const strength = Number(value);
					if (Number.isFinite(strength)) this.value.strength = strength;
					callbacks.change();
					node.setDirtyCanvas?.(true, true);
				}, event);
				return true;
			} else {
				showSlotEditor(this, callbacks.change);
				return true;
			}
			callbacks.change();
			node.setDirtyCanvas?.(true, true);
			return true;
		},
	};
}

function makeBaseOrderRow(callbacks, strength = 1, translate = false, enabled = true) {
	return {
		type: "jh_base_prompt_row",
		name: "jh_base_prompt_row",
		serialize: false,
		value: { strength, translate, enabled },
		bounds: {},
		computeSize(width) {
			return [width || 0, 28];
		},
		draw(ctx, node, width, y) {
			const x = 12;
			const rowWidth = width - 24;
			this.bounds.toggle = [x + 6, y + 6, 12, 12];
			this.bounds.up = [x + rowWidth - 116, y + 2, 20, 20];
			this.bounds.down = [x + rowWidth - 94, y + 2, 20, 20];
			this.bounds.translate = [x + rowWidth - 142, y + 2, 22, 20];
			this.bounds.reset = [x + rowWidth - 166, y + 2, 22, 20];
			this.bounds.dec = [x + rowWidth - 70, y + 2, 18, 20];
			this.bounds.strength = [x + rowWidth - 52, y + 2, 34, 20];
			this.bounds.inc = [x + rowWidth - 18, y + 2, 18, 20];
			this.bounds.row = [x, y, rowWidth, 24];
			ctx.save();
			ctx.fillStyle = "#33445c";
			ctx.strokeStyle = "#91a9d0";
			ctx.beginPath();
			ctx.roundRect(x, y, rowWidth, 24, 7);
			ctx.fill();
			ctx.stroke();
			ctx.fillStyle = this.value.enabled ? "#91a9d0" : "#666";
			ctx.beginPath();
			ctx.arc(x + 12, y + 12, 6, 0, Math.PI * 2);
			ctx.fill();
			ctx.globalAlpha = this.value.enabled ? 1 : 0.45;
			ctx.fillStyle = "#e4ecf7";
			ctx.font = "600 12px sans-serif";
			ctx.textBaseline = "middle";
			ctx.textAlign = "left";
			ctx.fillText("Base Prompt", x + 24, y + 12);
			ctx.textAlign = "center";
			ctx.fillStyle = "#aaa";
			ctx.font = "13px sans-serif";
			ctx.fillText("\u21ba", this.bounds.reset[0] + 11, y + 12);
			ctx.fillStyle = this.value.translate ? "#7ed6a5" : "#777";
			ctx.font = "600 9px sans-serif";
			ctx.fillText("EN", this.bounds.translate[0] + 11, y + 12);
			ctx.fillStyle = "#e4ecf7";
			ctx.font = "12px sans-serif";
			ctx.fillText("\u2191", this.bounds.up[0] + 10, y + 12);
			ctx.fillText("\u2193", this.bounds.down[0] + 10, y + 12);
			ctx.fillText("-", this.bounds.dec[0] + 9, y + 12);
			ctx.font = "10px sans-serif";
			ctx.fillText(this.value.strength.toFixed(2), this.bounds.strength[0] + 17, y + 12);
			ctx.font = "12px sans-serif";
			ctx.fillText("+", this.bounds.inc[0] + 9, y + 12);
			ctx.restore();
		},
		mouse(event, pos, node) {
			if (event.type !== "pointerdown" || !contains(this.bounds.row, pos)) return false;
			if (contains(this.bounds.toggle, pos)) {
				this.value.enabled = !this.value.enabled;
				callbacks.change();
			}
			else if (contains(this.bounds.up, pos)) callbacks.move(this, -1);
			else if (contains(this.bounds.down, pos)) callbacks.move(this, 1);
			else if (contains(this.bounds.reset, pos)) {
				this.value.translate = false;
				this.value.strength = 1;
				callbacks.change();
			}
			else if (contains(this.bounds.translate, pos)) {
				this.value.translate = !this.value.translate;
				callbacks.change();
			}
			else if (contains(this.bounds.dec, pos)) {
				this.value.strength = Math.round((this.value.strength - 0.05) * 100) / 100;
				callbacks.change();
			} else if (contains(this.bounds.inc, pos)) {
				this.value.strength = Math.round((this.value.strength + 0.05) * 100) / 100;
				callbacks.change();
			} else if (contains(this.bounds.strength, pos)) {
				app.canvas.prompt("Base prompt strength", this.value.strength, (value) => {
					const parsed = Number(value);
					if (Number.isFinite(parsed)) this.value.strength = parsed;
					callbacks.change();
					node.setDirtyCanvas?.(true, true);
				}, event);
			}
			else return false;
			return true;
		},
	};
}

function makeInputOrderRow(name, callbacks, strength = 1, translate = false, enabled = true) {
	const number = name.match(/(\d+)$/)?.[1] || "";
	return {
		type: "jh_input_prompt_row",
		name: `jh_input_prompt_row_${name}`,
		inputName: name,
		serialize: false,
		value: { strength, translate, enabled },
		bounds: {},
		computeSize(width) {
			return [width || 0, 28];
		},
		draw(ctx, node, width, y) {
			const x = 12;
			const rowWidth = width - 24;
			this.bounds.toggle = [x + 6, y + 6, 12, 12];
			this.bounds.up = [x + rowWidth - 116, y + 2, 20, 20];
			this.bounds.down = [x + rowWidth - 94, y + 2, 20, 20];
			this.bounds.translate = [x + rowWidth - 142, y + 2, 22, 20];
			this.bounds.reset = [x + rowWidth - 166, y + 2, 22, 20];
			this.bounds.dec = [x + rowWidth - 70, y + 2, 18, 20];
			this.bounds.strength = [x + rowWidth - 52, y + 2, 34, 20];
			this.bounds.inc = [x + rowWidth - 18, y + 2, 18, 20];
			this.bounds.row = [x, y, rowWidth, 24];
			ctx.save();
			ctx.fillStyle = "#29493d";
			ctx.strokeStyle = "#71b99d";
			ctx.beginPath();
			ctx.roundRect(x, y, rowWidth, 24, 7);
			ctx.fill();
			ctx.stroke();
			ctx.fillStyle = this.value.enabled ? "#71b99d" : "#666";
			ctx.beginPath();
			ctx.arc(x + 12, y + 12, 6, 0, Math.PI * 2);
			ctx.fill();
			ctx.globalAlpha = this.value.enabled ? 1 : 0.45;
			ctx.fillStyle = "#dff5ec";
			ctx.font = "600 12px sans-serif";
			ctx.textBaseline = "middle";
			ctx.textAlign = "left";
			ctx.fillText(`Input Prompt ${number}`, x + 24, y + 12);
			ctx.textAlign = "center";
			ctx.fillStyle = "#aaa";
			ctx.font = "13px sans-serif";
			ctx.fillText("\u21ba", this.bounds.reset[0] + 11, y + 12);
			ctx.fillStyle = this.value.translate ? "#7ed6a5" : "#777";
			ctx.font = "600 9px sans-serif";
			ctx.fillText("EN", this.bounds.translate[0] + 11, y + 12);
			ctx.fillStyle = "#dff5ec";
			ctx.font = "12px sans-serif";
			ctx.fillText("\u2191", this.bounds.up[0] + 10, y + 12);
			ctx.fillText("\u2193", this.bounds.down[0] + 10, y + 12);
			ctx.fillText("-", this.bounds.dec[0] + 9, y + 12);
			ctx.font = "10px sans-serif";
			ctx.fillText(this.value.strength.toFixed(2), this.bounds.strength[0] + 17, y + 12);
			ctx.font = "12px sans-serif";
			ctx.fillText("+", this.bounds.inc[0] + 9, y + 12);
			ctx.restore();
		},
		mouse(event, pos, node) {
			if (event.type !== "pointerdown" || !contains(this.bounds.row, pos)) return false;
			if (contains(this.bounds.toggle, pos)) {
				this.value.enabled = !this.value.enabled;
				callbacks.change();
			}
			else if (contains(this.bounds.up, pos)) callbacks.move(this, -1);
			else if (contains(this.bounds.down, pos)) callbacks.move(this, 1);
			else if (contains(this.bounds.reset, pos)) {
				this.value.translate = false;
				this.value.strength = 1;
				callbacks.change();
			}
			else if (contains(this.bounds.translate, pos)) {
				this.value.translate = !this.value.translate;
				callbacks.change();
			}
			else if (contains(this.bounds.dec, pos)) {
				this.value.strength = Math.round((this.value.strength - 0.05) * 100) / 100;
				callbacks.change();
			} else if (contains(this.bounds.inc, pos)) {
				this.value.strength = Math.round((this.value.strength + 0.05) * 100) / 100;
				callbacks.change();
			} else if (contains(this.bounds.strength, pos)) {
				app.canvas.prompt("Input prompt strength", this.value.strength, (value) => {
					const parsed = Number(value);
					if (Number.isFinite(parsed)) this.value.strength = parsed;
					callbacks.change();
					node.setDirtyCanvas?.(true, true);
				}, event);
			}
			else return false;
			return true;
		},
	};
}

function installPromptBuilder(node) {
	if (node.jhPromptBuilderInstalled) return;
	node.jhPromptBuilderInstalled = true;
	const storedWidget = getWidget(node, "prompt_slots");
	const basePositionWidget = getWidget(node, "base_position");
	const promptOrderWidget = getWidget(node, "prompt_order");
	const baseStrengthWidget = getWidget(node, "base_strength");
	const inputStrengthsWidget = getWidget(node, "input_prompt_strengths");
	const baseTranslateWidget = getWidget(node, "base_translate");
	const inputTranslationsWidget = getWidget(node, "input_prompt_translations");
	const baseEnabledWidget = getWidget(node, "base_enabled");
	const inputEnabledWidget = getWidget(node, "input_prompt_enabled");
	if (!storedWidget || !basePositionWidget || !promptOrderWidget || !baseStrengthWidget || !inputStrengthsWidget || !baseTranslateWidget || !inputTranslationsWidget || !baseEnabledWidget || !inputEnabledWidget) return;
	hideWidget(storedWidget);
	hideWidget(basePositionWidget);
	hideWidget(promptOrderWidget);
	hideWidget(baseStrengthWidget);
	hideWidget(inputStrengthsWidget);
	hideWidget(baseTranslateWidget);
	hideWidget(inputTranslationsWidget);
	hideWidget(baseEnabledWidget);
	hideWidget(inputEnabledWidget);
	node.jhPromptSlots = [];
	node.jhInputPromptRows = new Map();

	const resizeNode = () => {
		const size = node.computeSize?.();
		if (size) node.onResize?.([Math.max(node.size[0], 360), size[1]]);
		node.setDirtyCanvas?.(true, true);
	};
	const sync = () => {
		const slots = node.jhPromptSlots.map((slot) => ({ ...slot.value }));
		setStoredValue(node, storedWidget, JSON.stringify(slots));
		setStoredValue(node, basePositionWidget, node.jhPromptItems.indexOf(baseRow));
		const order = node.jhPromptItems.map((item) => {
			if (item === baseRow) return "base";
			if (item.type === "jh_input_prompt_row") return `input:${item.inputName}`;
			return `slot:${item.value.id}`;
		});
		setStoredValue(node, promptOrderWidget, JSON.stringify(order));
		setStoredValue(node, baseStrengthWidget, baseRow.value.strength);
		const inputStrengths = Object.fromEntries([...node.jhInputPromptRows].map(([name, row]) => [name, row.value.strength]));
		setStoredValue(node, inputStrengthsWidget, JSON.stringify(inputStrengths));
		setStoredValue(node, baseTranslateWidget, baseRow.value.translate);
		const inputTranslations = Object.fromEntries([...node.jhInputPromptRows].map(([name, row]) => [name, row.value.translate]));
		setStoredValue(node, inputTranslationsWidget, JSON.stringify(inputTranslations));
		setStoredValue(node, baseEnabledWidget, baseRow.value.enabled);
		const inputEnabled = Object.fromEntries([...node.jhInputPromptRows].map(([name, row]) => [name, row.value.enabled]));
		setStoredValue(node, inputEnabledWidget, JSON.stringify(inputEnabled));
	};
	const layoutRows = () => {
		for (const item of [baseRow, ...node.jhPromptSlots, ...node.jhInputPromptRows.values()]) {
			const widgetIndex = node.widgets.indexOf(item);
			if (widgetIndex >= 0) node.widgets.splice(widgetIndex, 1);
		}
		const buttonIndex = node.widgets.indexOf(addButton);
		node.widgets.splice(buttonIndex, 0, ...node.jhPromptItems);
	};
	const remove = (slot) => {
		detachWidget(node, slot);
		node.jhPromptSlots = node.jhPromptSlots.filter((item) => item !== slot);
		node.jhPromptItems = node.jhPromptItems.filter((item) => item !== slot);
		sync();
		resizeNode();
	};
	const move = (item, direction) => {
		const index = node.jhPromptItems.indexOf(item);
		const nextIndex = index + direction;
		if (index < 0 || nextIndex < 0 || nextIndex >= node.jhPromptItems.length) return;
		[node.jhPromptItems[index], node.jhPromptItems[nextIndex]] = [node.jhPromptItems[nextIndex], node.jhPromptItems[index]];
		node.jhPromptSlots = node.jhPromptItems.filter((entry) => entry.type === "jh_prompt_row");
		layoutRows();
		sync();
		node.setDirtyCanvas?.(true, true);
	};
	const callbacks = { change: sync, remove, move };
	const initialBaseStrength = Number(baseStrengthWidget.value);
	const baseRow = makeBaseOrderRow(callbacks, Number.isFinite(initialBaseStrength) ? initialBaseStrength : 1, baseTranslateWidget.value === true, baseEnabledWidget.value !== false);
	const getInputStrengths = () => {
		try {
			const strengths = JSON.parse(inputStrengthsWidget.value || "{}");
			return strengths && typeof strengths === "object" && !Array.isArray(strengths) ? strengths : {};
		} catch {
			return {};
		}
	};
	const getInputTranslations = () => {
		try {
			const translations = JSON.parse(inputTranslationsWidget.value || "{}");
			return translations && typeof translations === "object" && !Array.isArray(translations) ? translations : {};
		} catch {
			return {};
		}
	};
	const getInputEnabled = () => {
		try {
			const enabled = JSON.parse(inputEnabledWidget.value || "{}");
			return enabled && typeof enabled === "object" && !Array.isArray(enabled) ? enabled : {};
		} catch {
			return {};
		}
	};
	const applyStoredOrder = () => {
		const itemsByToken = new Map(node.jhPromptItems.map((item) => {
			if (item === baseRow) return ["base", item];
			if (item.type === "jh_input_prompt_row") return [`input:${item.inputName}`, item];
			return [`slot:${item.value.id}`, item];
		}));
		let storedOrder = [];
		try {
			storedOrder = JSON.parse(promptOrderWidget.value || "[]");
		} catch {
			storedOrder = [];
		}
		if (!Array.isArray(storedOrder) || !storedOrder.length) return;
		const ordered = storedOrder.map((token) => itemsByToken.get(token)).filter(Boolean);
		ordered.push(...node.jhPromptItems.filter((item) => !ordered.includes(item)));
		node.jhPromptItems = ordered;
		node.jhPromptSlots = ordered.filter((item) => item.type === "jh_prompt_row");
	};
	const refreshInputRows = (shouldSync = false) => {
		const connectedNames = new Set();
		const storedInputStrengths = getInputStrengths();
		const storedInputTranslations = getInputTranslations();
		const storedInputEnabled = getInputEnabled();
		for (const input of node.inputs || []) {
			const inputName = input.name?.split(".").at(-1) || "";
			const match = inputName.match(/^input_prompt(\d+)$/);
			if (!match) continue;
			input.label = `Input Prompt ${match[1]}`;
			if (input.link != null) connectedNames.add(inputName);
		}
		for (const [name, row] of node.jhInputPromptRows) {
			if (connectedNames.has(name)) continue;
			detachWidget(node, row);
			node.jhInputPromptRows.delete(name);
			node.jhPromptItems = node.jhPromptItems.filter((item) => item !== row);
		}
		for (const name of connectedNames) {
			if (node.jhInputPromptRows.has(name)) {
				const row = node.jhInputPromptRows.get(name);
				if (Number.isFinite(Number(storedInputStrengths[name]))) row.value.strength = Number(storedInputStrengths[name]);
				if (typeof storedInputTranslations[name] === "boolean") row.value.translate = storedInputTranslations[name];
				if (typeof storedInputEnabled[name] === "boolean") row.value.enabled = storedInputEnabled[name];
				if (!node.jhPromptItems.includes(row)) node.jhPromptItems.push(row);
				continue;
			}
			const strength = Number.isFinite(Number(storedInputStrengths[name])) ? Number(storedInputStrengths[name]) : 1;
			const row = makeInputOrderRow(name, callbacks, strength, storedInputTranslations[name] === true, storedInputEnabled[name] !== false);
			node.addCustomWidget(row);
			node.jhInputPromptRows.set(name, row);
			node.jhPromptItems.push(row);
		}
		applyStoredOrder();
		layoutRows();
		if (shouldSync) sync();
		resizeNode();
	};
	const addSlot = (data = {}, shouldSync = true) => {
		const slot = makePromptRow(data, callbacks);
		node.addCustomWidget(slot);
		node.jhPromptSlots.push(slot);
		node.jhPromptItems.push(slot);
		layoutRows();
		if (shouldSync) sync();
		resizeNode();
	};
	const rebuild = () => {
		for (const slot of node.jhPromptSlots) {
			detachWidget(node, slot);
		}
		node.jhPromptSlots = [];
		baseRow.value.strength = Number.isFinite(Number(baseStrengthWidget.value)) ? Number(baseStrengthWidget.value) : 1;
		baseRow.value.translate = baseTranslateWidget.value === true;
		baseRow.value.enabled = baseEnabledWidget.value !== false;
		node.jhPromptItems = [baseRow];
		for (const data of parseSlots(storedWidget.value)) {
			const slot = makePromptRow(data, callbacks);
			node.addCustomWidget(slot);
			node.jhPromptSlots.push(slot);
			node.jhPromptItems.push(slot);
		}
		const basePosition = Math.max(0, Math.min(Number(basePositionWidget.value) || 0, node.jhPromptSlots.length));
		node.jhPromptItems.splice(0, 1);
		node.jhPromptItems.splice(basePosition, 0, baseRow);
		refreshInputRows(false);
	};
	const addButton = node.addWidget("button", "+ Add Prompt", "+ Add Prompt", () => addSlot(), { serialize: false });
	addButton.serialize = false;
	node.addCustomWidget(baseRow);
	node.jhPromptItems = [baseRow];
	const originalOnConnectionsChange = node.onConnectionsChange;
	node.onConnectionsChange = function () {
		originalOnConnectionsChange?.apply(this, arguments);
		requestAnimationFrame(() => requestAnimationFrame(() => refreshInputRows(true)));
	};

	const originalGetSlotInPosition = node.getSlotInPosition?.bind(node);
	const originalGetSlotMenuOptions = node.getSlotMenuOptions?.bind(node);
	node.getSlotInPosition = function (canvasX, canvasY) {
		const slot = originalGetSlotInPosition?.(canvasX, canvasY);
		if (slot || canvasX < this.pos[0] || canvasX > this.pos[0] + this.size[0]) return slot;
		for (const widget of this.jhPromptSlots || []) {
			const top = this.pos[1] + (widget.last_y ?? -1000);
			if (canvasY >= top && canvasY <= top + 28) return { widget, output: { type: "JH_PROMPT_ROW" } };
		}
		return slot;
	};
	node.getSlotMenuOptions = function (slot) {
		if (slot?.widget?.type === "jh_prompt_row") {
			return [{ content: "Remove Prompt", callback: () => remove(slot.widget) }];
		}
		return originalGetSlotMenuOptions?.(slot);
	};

	const originalOnConfigure = node.onConfigure;
	node.onConfigure = function () {
		originalOnConfigure?.apply(this, arguments);
		requestAnimationFrame(() => {
			hideWidget(getWidget(this, "prompt_slots"));
			hideWidget(getWidget(this, "base_position"));
			hideWidget(getWidget(this, "prompt_order"));
			hideWidget(getWidget(this, "base_strength"));
			hideWidget(getWidget(this, "input_prompt_strengths"));
			hideWidget(getWidget(this, "base_translate"));
			hideWidget(getWidget(this, "input_prompt_translations"));
			hideWidget(getWidget(this, "base_enabled"));
			hideWidget(getWidget(this, "input_prompt_enabled"));
			rebuild();
		});
	};
	rebuild();
	requestAnimationFrame(() => requestAnimationFrame(() => refreshInputRows(false)));
}

app.registerExtension({
	name: "jh.prompt.builder",
	loadedGraphNode(node) {
		if (node.comfyClass === "JHPromptBuilder") installPromptBuilder(node);
	},
	nodeCreated(node) {
		if (node.comfyClass === "JHPromptBuilder") installPromptBuilder(node);
	},
});
