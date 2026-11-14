// The @ material list of the prompt boxes: typing @ (or the full-width ＠ an IME gives) lists the materials,
// typing on filters them, and picking one hands the item and the `@query` range to the caller, which writes the
// tag. No timeline knowledge lives here.

// `@` or `＠` before the caret with no whitespace in between, unless it sits inside a `<…` still open: a hand-written
// `<@name>`, or any tag the inserted one would cut in two.
export function mentionQuery(text, caret) {
    for (let i = caret - 1; i >= 0; i--) {
        const ch = text[i];
        if (/\s/.test(ch)) return null;
        if (ch === "@" || ch === "＠") {
            const before = text.slice(0, i);
            return before.lastIndexOf("<") > before.lastIndexOf(">") ? null : { start: i, end: caret, query: text.slice(i + 1, caret) };
        }
    }
    return null;
}

export function filterMentions(items, query) {
    const q = query.normalize("NFC").toLowerCase();
    return items.filter((item) => item.name.normalize("NFC").toLowerCase().includes(q));
}

const MIRRORED = ["boxSizing", "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth", "paddingTop",
    "paddingRight", "paddingBottom", "paddingLeft", "fontFamily", "fontSize", "fontWeight", "fontStyle", "letterSpacing",
    "lineHeight", "textTransform", "wordSpacing", "tabSize"];

// Where character `index` sits inside the textarea, in its own (unscaled) CSS pixels: a hidden copy of the box
// with the same text wrapping, measured at a marker.
function caretOffset(textarea, index) {
    const style = getComputedStyle(textarea);
    const mirror = document.createElement("div");
    for (const p of MIRRORED) mirror.style[p] = style[p];
    Object.assign(mirror.style, { position: "absolute", visibility: "hidden", top: "0", left: "-9999px",
        whiteSpace: "pre-wrap", overflowWrap: "break-word", width: `${textarea.offsetWidth}px` });
    mirror.textContent = textarea.value.slice(0, index);
    const mark = document.createElement("span");
    mark.textContent = "​";
    mirror.append(mark);
    document.body.append(mirror);
    const out = { left: mark.offsetLeft - textarea.scrollLeft, top: mark.offsetTop - textarea.scrollTop,
        height: mark.offsetHeight || parseFloat(style.lineHeight) || 16 };
    mirror.remove();
    return out;
}

// Fixed on the page, not inside the editor: the editor clips its overflow and the canvas scales it, so the
// caret is converted to screen pixels and the list opens above the caret when there is no room below.
function place(popup, textarea, index) {
    const box = textarea.getBoundingClientRect();
    const scale = textarea.offsetWidth ? box.width / textarea.offsetWidth : 1;
    const c = caretOffset(textarea, index);
    const left = Math.min(box.left + c.left * scale, window.innerWidth - popup.offsetWidth - 4);
    const below = box.top + (c.top + c.height) * scale + 2;
    const top = below + popup.offsetHeight > window.innerHeight ? box.top + c.top * scale - popup.offsetHeight - 2 : below;
    popup.style.left = `${Math.max(0, left)}px`;
    popup.style.top = `${Math.max(0, top)}px`;
}

// items(): [{key, name, label, thumb?, icon?, status, tone?, disabled, title?, ...caller data}] for the current box;
// pick(item, {start, end}) writes it. → {close(), isOpen()}
export function attachMention(textarea, host, { items, pick, hint }) {
    let open = null;   // {el, list, index, range}

    const close = () => {
        open?.el.remove();
        open = null;
    };
    const enabled = (i) => open.list[i] && !open.list[i].disabled;
    const choose = (item) => {
        const range = { start: open.range.start, end: open.range.end };
        close();
        pick(item, range);
    };
    const draw = () => {
        const rows = open.list.map((item, i) => {
            const row = document.createElement("div");
            row.className = `row${i === open.index ? " on" : ""}${item.disabled ? " off" : ""}`;
            if (item.title) row.setAttribute("title", item.title);
            const icon = document.createElement(item.thumb ? "img" : "span");
            if (item.thumb) {
                icon.setAttribute("src", item.thumb);
                icon.setAttribute("alt", "");
            } else {
                icon.className = "icon";
                icon.textContent = item.icon ?? "";
            }
            const name = document.createElement("span");
            name.className = "name";
            name.textContent = item.label;
            const status = document.createElement("span");
            status.className = `status${item.tone ? ` ${item.tone}` : ""}`;
            status.textContent = item.status;
            row.append(icon, name, status);
            // keep the caret in the box: a press on the list must not blur it
            row.addEventListener("pointerdown", (e) => e.preventDefault());
            row.addEventListener("mousedown", (e) => e.preventDefault());
            row.addEventListener("click", () => !item.disabled && choose(item));
            return row;
        });
        const foot = document.createElement("div");
        foot.className = "foot";
        foot.textContent = hint;
        open.el.replaceChildren(...rows, foot);
        rows[open.index]?.scrollIntoView?.({ block: "nearest" });
    };
    const refresh = () => {
        const collapsed = textarea.selectionStart === textarea.selectionEnd;
        const q = collapsed ? mentionQuery(textarea.value, textarea.selectionStart) : null;
        const list = q ? filterMentions(items() ?? [], q.query) : [];
        if (!list.length) return close();
        if (!open) {
            const el = document.createElement("div");
            el.className = "mf-tl-mention";
            host.append(el);
            open = { el };
        }
        open.list = list;
        open.range = q;
        open.index = list.findIndex((item) => !item.disabled);
        draw();
        place(open.el, textarea, q.start);
    };
    const move = (step) => {
        for (let i = open.index + step; i >= 0 && i < open.list.length; i += step) {
            if (enabled(i)) {
                open.index = i;
                return draw();
            }
        }
    };

    textarea.addEventListener("input", (e) => !e.isComposing && refresh());
    textarea.addEventListener("compositionend", refresh);
    textarea.addEventListener("click", refresh);
    textarea.addEventListener("keyup", (e) => ["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key) && refresh());
    textarea.addEventListener("blur", close);
    textarea.addEventListener("keydown", (e) => {
        if (!open || e.isComposing || e.keyCode === 229) return;
        if (e.key === "ArrowDown" || e.key === "ArrowUp") move(e.key === "ArrowDown" ? 1 : -1);
        else if ((e.key === "Enter" || e.key === "Tab") && enabled(open.index)) choose(open.list[open.index]);
        else if (e.key === "Escape") close();
        else return;
        e.preventDefault();
        e.stopPropagation();
    });
    return { close, isOpen: () => !!open };
}
