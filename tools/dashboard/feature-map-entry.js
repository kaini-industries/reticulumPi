import "../../src/reticulumpi/builtin_plugins/web_dashboard/static/map.js";
import "../../src/reticulumpi/builtin_plugins/web_dashboard/static/node_tracker.js";

export function init(context) {
  context.replay("map");
}

export function dispose() {
  if (window.RPI && window.RPI.disposeMapFeature) {
    window.RPI.disposeMapFeature();
  }
}
