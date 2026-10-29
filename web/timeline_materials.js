// MediaForge timeline v2 materials — pure functions shared by the timeline editor.
//
// Mirrors utils/timeline_materials.py (Python stays the authority and re-validates on every
// run); both sides are checked against tests/golden/timeline_materials_vectors.json.

// JSON.parse silently keeps the last duplicate key and turns 5.0 into 5, so the editor could
// show — and later save — a timeline Python rejects. strictParse() rejects duplicate keys like
// Python's object_pairs_hook does (raw level, location "timeline_data"), and wraps non-integer
// number tokens in FloatToken so the field check fails at the same location Python's int check
// does. Every number in timeline_data is an integer, so no FloatToken is ever valid.
export class FloatToken {
    constructor(text) {
        this.text = text;
    }
}

export function strictParse(text) {
    let i = 0;
    const fail = (what) => {
        throw new SyntaxError(`${what} at ${i}`);
    };
    const ws = () => {
        while (i < text.length && " \t\n\r".includes(text[i])) i++;
    };
    const literal = (word, value) => {
        if (text.startsWith(word, i)) {
            i += word.length;
            return value;
        }
        return fail("unexpected token");
    };
    const string = () => {
        i++; // opening quote
        let out = "";
        for (;;) {
            if (i >= text.length) fail("unterminated string");
            const c = text[i++];
            if (c === '"') return out;
            if (c < " ") fail("control character in string");
            if (c !== "\\") {
                out += c;
                continue;
            }
            const e = text[i++];
            const simple = { '"': '"', "\\": "\\", "/": "/", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" }[e];
            if (simple !== undefined) out += simple;
            else if (e === "u" && /^[0-9a-fA-F]{4}$/.test(text.slice(i, i + 4))) {
                out += String.fromCharCode(parseInt(text.slice(i, i + 4), 16));
                i += 4;
            } else fail("bad escape");
        }
    };
    const number = () => {
        const m = /^-?(?:0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?/.exec(text.slice(i));
        if (!m) fail("bad number");
        i += m[0].length;
        return m[1] || m[2] ? new FloatToken(m[0]) : Number(m[0]);
    };
    const value = () => {
        ws();
        const c = text[i];
        if (c === "{") {
            i++;
            const obj = {};
            const seen = new Set();
            ws();
            if (text[i] === "}") {
                i++;
                return obj;
            }
            for (;;) {
                ws();
                if (text[i] !== '"') fail("expected key");
                const key = string();
                if (seen.has(key)) fail(`duplicate key ${key}`);
                seen.add(key);
                ws();
                if (text[i++] !== ":") fail("expected ':'");
                // defineProperty: plain assignment of "__proto__" would swap the prototype instead of
                // adding a key, hiding an unknown field that Python rejects
                Object.defineProperty(obj, key, { value: value(), enumerable: true, writable: true, configurable: true });
                ws();
                const d = text[i++];
                if (d === "}") return obj;
                if (d !== ",") fail("expected ',' or '}'");
            }
        }
        if (c === "[") {
            i++;
            const arr = [];
            ws();
            if (text[i] === "]") {
                i++;
                return arr;
            }
            for (;;) {
                arr.push(value());
                ws();
                const d = text[i++];
                if (d === "]") return arr;
                if (d !== ",") fail("expected ',' or ']'");
            }
        }
        if (c === '"') return string();
        if (c === "t") return literal("true", true);
        if (c === "f") return literal("false", false);
        if (c === "n") return literal("null", null);
        return number();
    };
    const out = value();
    ws();
    if (i !== text.length) fail("trailing data");
    return out;
}
