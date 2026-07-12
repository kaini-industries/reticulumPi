import "../../src/reticulumpi/builtin_plugins/web_dashboard/static/messages_panel.js";
import "../../src/reticulumpi/builtin_plugins/web_dashboard/static/messages_lxmf.js";
import "../../src/reticulumpi/builtin_plugins/web_dashboard/static/mqtt_feed.js";
import "../../src/reticulumpi/builtin_plugins/web_dashboard/static/messages_meshtastic_lora.js";
import "../../src/reticulumpi/builtin_plugins/web_dashboard/static/messages_meshcore.js";

export function init(context) {
  if (window.RPI && window.RPI.initMessagesFeature) {
    window.RPI.initMessagesFeature();
  }
  context.replay("messages");
}

export function dispose() {
  if (window.RPI && window.RPI.disposeMessagesFeature) {
    window.RPI.disposeMessagesFeature();
  }
}
