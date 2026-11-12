// MediaForge frontend extension: keeps the nodes that only exist for graph expansion out of the menus.
//
// 🎛️ Sample Segments builds these three per segment when it runs, so they have to stay registered. The Python
// side marks them DEPRECATED, which keeps them out of the node search and the node library; the right-click
// "Add Node" menu ignores that flag and reads LiteGraph's skip_list instead.

import { app } from "../../scripts/app.js";

const INTERNAL = new Set(["MF_EncodeH3Segment", "MF_ExtendLatent", "MF_AppendSegment"]);

app.registerExtension({
    name: "MediaForge.InternalNodes",
    beforeRegisterNodeDef(nodeType, nodeData) {
        if (INTERNAL.has(nodeData.name)) nodeType.skip_list = true;
    },
});
