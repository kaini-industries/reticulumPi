import "../../src/reticulumpi/builtin_plugins/web_dashboard/static/space.js";

export function init(context) {
  context.replay("space");
}

export function dispose() {
  if (window.RPI && window.RPI.disposeSpaceFeature) {
    window.RPI.disposeSpaceFeature();
  }
}
