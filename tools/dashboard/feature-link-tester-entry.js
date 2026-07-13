import "../../src/reticulumpi/builtin_plugins/web_dashboard/static/link_tester.js";

export function init(context) {
  if (window.RPI && window.RPI.initLinkTesterFeature) {
    window.RPI.initLinkTesterFeature();
  }
  context.replay("link-tester");
}

export function dispose() {}
