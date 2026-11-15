/**
 * The rules that govern a processor's controls, and the one place a hand edit changes them.
 *
 * A control's value over time comes from rules: automation lanes, recorded events and (for processor
 * parameters) a macro mapping from Intensity. `applyParameterEdit` is what the workbench's parameter setter does to
 * the patch, factored out so the TouchDesigner rule sync (osc-control.mjs, `OscFeedback`) and the tests exercise the
 * same code the page runs: while recording, an edit becomes an event; otherwise it removes the control's events, its
 * automation lane and its macro mapping, and sets the base value.
 */

/** An event within this many seconds of the last event on the same control replaces its value instead of adding one. */
export const EVENT_MERGE_SECONDS = 0.05;

/**
 * @param patch  the patch (events live on it)
 * @param node   the processor node
 * @param path   'intensity' | 'wet' | 'bypass' | 'solo' | 'params.<key>'
 * @param value  the new value
 * @param options {recording, time}
 * @returns the patch's events array after the edit (patch.events is replaced when the edit is not a recording)
 */
export function applyParameterEdit(patch, node, path, value, {recording = false, time = 0} = {}) {
  if (recording) {
    const last = patch.events.at(-1);
    if (last && last.nodeId === node.id && last.parameter === path && Math.abs(last.time - time) < EVENT_MERGE_SECONDS) last.value = value;
    else patch.events.push({time, nodeId: node.id, parameter: path, value});
    patch.events.sort((a, b) => a.time - b.time);
  } else {
    patch.events = patch.events.filter(event => !(event.nodeId === node.id && event.parameter === path));
    node.automation = node.automation.filter(lane => lane.parameter !== path);
    if (path.startsWith('params.')) {
      node.params[path.slice(7)] = value;
      node.macro = node.macro.filter(mapping => mapping.parameter !== path.slice(7));
    } else node[path] = value;
  }
  return patch.events;
}

/** The rules of one processor as the workbench holds them: {events, automation, macro}. Read-only copies. */
export function processorRules(patch, node) {
  return {
    events: (patch.events ?? []).filter(event => event.nodeId === node.id).map(event => ({time: event.time, parameter: event.parameter, value: event.value})),
    automation: (node.automation ?? []).map(lane => ({parameter: lane.parameter, interpolation: lane.interpolation, keyframes: lane.keyframes.map(frame => ({time: frame.time, value: frame.value}))})),
    macro: (node.macro ?? []).map(mapping => ({parameter: mapping.parameter, min: mapping.min, max: mapping.max, curve: mapping.curve})),
  };
}
