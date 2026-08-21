import { callable } from "@decky/api";

export interface Brand {
  id: string;
  label: string;
}

export interface Tv {
  host: string;
  name: string;
  brand: string;
  inputs?: Input[];
}

export interface Rule {
  display_id: string;
  host: string;
  input_id: string;
  enabled: boolean;
}

export interface Display {
  connector: string;
  id: string;
}

export interface Input {
  id: string;
  label: string;
}

export interface Triggers {
  connect: boolean;
  wake: boolean;
  home: boolean;
}

// Nudge the TV's volume after a stretch of silence, so an ARC soundbar doesn't fall asleep and
// clip the next sound. `dbfs` is the level below which the machine's own audio output counts as
// silent (decibels relative to digital full scale, so always negative).
export interface AudioKeepalive {
  enabled: boolean;
  seconds: number;
  dbfs: number;
}

// Live measurement behind the settings above, so the floor can be calibrated against real gear.
// `silent_seconds` is null while nothing is being measured; `unavailable` says why.
export interface AudioStatus {
  running: boolean;
  unavailable: string;
  silent_seconds: number | null;
  peak_dbfs: number;
}

export interface DiscoveredTv {
  host: string;
  name: string;
}

export const listBrands = callable<[], Brand[]>("list_brands");
export const listTvs = callable<[], Tv[]>("list_tvs");
export const getSelectedTv = callable<[], string>("get_selected_tv");
export const setSelectedTv = callable<[host: string], void>("set_selected_tv");
export const listRules = callable<[], Rule[]>("list_rules");
export const getTriggers = callable<[], Triggers>("get_triggers");
export const setTrigger = callable<[name: string, enabled: boolean], void>("set_trigger");
export const setStreaming = callable<[active: boolean], void>("set_streaming");
export const autoSwitchPaused = callable<[], boolean>("auto_switch_paused");
export const getPauseWhenStreaming = callable<[], boolean>("get_pause_when_streaming");
export const setPauseWhenStreaming = callable<[enabled: boolean], void>("set_pause_when_streaming");
export const getAudioKeepalive = callable<[], AudioKeepalive>("get_audio_keepalive");
export const setAudioKeepalive =
  callable<[enabled: boolean, seconds: number, dbfs: number], void>("set_audio_keepalive");
export const getAudioStatus = callable<[], AudioStatus>("get_audio_status");
export const getNotifications = callable<[], boolean>("get_notifications");
export const setNotifications = callable<[enabled: boolean], void>("set_notifications");
export const listDisplays = callable<[], Display[]>("list_displays");
export const getInputs = callable<[host: string], Input[]>("get_inputs");
export const discoverTvs = callable<[brand: string], DiscoveredTv[]>("discover_tvs");
export const pairTv = callable<[host: string, name: string, brand: string], Tv>("pair_tv");
export const removeTv = callable<[host: string], void>("remove_tv");
export const switchInput = callable<[host: string, inputId: string], void>("switch_input");
export const powerOffTv = callable<[host: string], void>("power_off_tv");
export const volumeUp = callable<[host: string], void>("volume_up");
export const volumeDown = callable<[host: string], void>("volume_down");
export const setRule =
  callable<[displayId: string, host: string, inputId: string, enabled: boolean], void>("set_rule");
export const removeRule = callable<[displayId: string], void>("remove_rule");
export const reapplyRules = callable<[], void>("reapply_rules");
export const isReachable = callable<[host: string], boolean>("is_reachable");
export const readLogs = callable<[], string>("read_logs");
export const clearLogs = callable<[], void>("clear_logs");

export const tvLabel = (tv: Tv): string => tv.name || tv.host;

// Invoke a backend callable, swallowing every failure. A missing method on a stale/mismatched
// backend can throw synchronously (not just reject), so the call itself is wrapped — not only
// the returned promise. Used wherever a newer-frontend callable must never break plugin load.
export const safeCall = (run: () => Promise<unknown> | undefined): void => {
  try {
    void run()?.catch(() => {});
  } catch {
    /* stale/mismatched backend — ignore */
  }
};
