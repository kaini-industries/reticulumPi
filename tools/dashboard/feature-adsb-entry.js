import "../../src/reticulumpi/builtin_plugins/web_dashboard/static/adsb.js";

export function init(context) {
  context.replay("adsb");
}

export function dispose() {
  if (window.RPI && window.RPI.disposeAdsbFeature) {
    window.RPI.disposeAdsbFeature();
  }
}
