import "../../src/reticulumpi/builtin_plugins/web_dashboard/static/radio.js";

export function init(context) {
  if (window.RPI && window.RPI.initRadioFeature) {
    window.RPI.initRadioFeature();
  }
  context.replay("radio");
}

export function dispose() {
  if (window.RPI && window.RPI.disposeRadioFeature) {
    window.RPI.disposeRadioFeature();
  }
}
