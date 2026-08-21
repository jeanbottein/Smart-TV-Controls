import { PanelSection, PanelSectionRow, SliderField, ToggleField } from "@decky/ui";
import { useEffect, useRef, useState } from "react";
import {
  getAudioKeepalive,
  getAudioStatus,
  safeCall,
  setAudioKeepalive,
  type AudioKeepalive as Settings,
  type AudioStatus,
} from "../api";
import { statusLine, statusLabel } from "./styles";

// An ARC soundbar drops into standby when the line has been silent for a while, and the first
// sound after that is clipped while the audio path wakes back up. With this on, the backend
// watches how loud this machine's own output actually is — read off the sink's monitor, never a
// microphone — and sends the TV a volume up/down pair once it has been quiet long enough. That
// pair is net-zero on the volume; the point is the CEC traffic the TV emits, which wakes the bar.
// It does flash the TV's volume OSD, which is why it defaults off.
//
// The floor is in dBFS (decibels below digital full scale, so always negative) because there is
// no absolute loudness to be had on this side of the HDMI cable — a soundbar's own dB scale
// depends on its volume knob. The live readout below is what makes it calibratable: watch the
// peak with the room quiet, then set the floor just above it.
const STATUS_POLL_MS = 1000;

const formatDuration = (seconds: number) => {
  const whole = Math.max(0, Math.floor(seconds));
  return whole < 60 ? `${whole}s` : `${Math.floor(whole / 60)}m${String(whole % 60).padStart(2, "0")}s`;
};

function Readout({ status }: { status: AudioStatus | null }) {
  if (!status) return null;
  const text = status.running
    ? `peak ${status.peak_dbfs.toFixed(1)} dBFS · quiet for ${formatDuration(status.silent_seconds ?? 0)}`
    : status.unavailable || "starting the monitor…";
  return (
    <PanelSectionRow>
      <div style={statusLine}>
        <span style={statusLabel}>Output</span>
        <span>{text}</span>
      </div>
    </PanelSectionRow>
  );
}

export function AudioKeepalive() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [status, setStatus] = useState<AudioStatus | null>(null);
  // The sliders fire onChange per step while being dragged; persist the final value only, or a
  // single drag writes the settings file a dozen times.
  const saveTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  // Hidden on a stale backend that lacks get_audio_keepalive, guarded like the rest of the
  // plugin's callable use (a missing method can throw synchronously, not just reject).
  useEffect(() => {
    safeCall(() => getAudioKeepalive().then(setSettings));
  }, []);

  // Only poll the live level while the feature is on — that is the only time a capture exists.
  useEffect(() => {
    if (!settings?.enabled) {
      setStatus(null);
      return;
    }
    let active = true;
    const load = () => safeCall(() => getAudioStatus().then((next) => active && setStatus(next)));
    load();
    const timer = setInterval(load, STATUS_POLL_MS);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [settings?.enabled]);

  useEffect(() => () => clearTimeout(saveTimer.current), []);

  if (!settings) return null;

  const update = (patch: Partial<Settings>, immediate: boolean) => {
    const next = { ...settings, ...patch };
    setSettings(next);
    clearTimeout(saveTimer.current);
    const save = () => safeCall(() => setAudioKeepalive(next.enabled, next.seconds, next.dbfs));
    if (immediate) save();
    else saveTimer.current = setTimeout(save, 500);
  };

  return (
    <PanelSection title="Audio keep-alive">
      <PanelSectionRow>
        <ToggleField
          label="Nudge the volume during silence"
          description="Sends the TV a volume +/− pair so an ARC soundbar doesn't sleep and clip the next sound"
          checked={settings.enabled}
          onChange={(on) => update({ enabled: on }, true)}
        />
      </PanelSectionRow>
      {settings.enabled ? (
        <>
          <PanelSectionRow>
            <SliderField
              label="After"
              description="How long the output must stay quiet before a nudge"
              value={Math.round(settings.seconds / 60)}
              min={1}
              max={15}
              step={1}
              showValue
              valueSuffix=" min"
              onChange={(minutes) => update({ seconds: minutes * 60 }, false)}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <SliderField
              label="Silence below"
              description="Anything quieter than this counts as no sound at all"
              value={settings.dbfs}
              min={-70}
              max={-30}
              step={1}
              showValue
              valueSuffix=" dBFS"
              onChange={(dbfs) => update({ dbfs }, false)}
            />
          </PanelSectionRow>
          <Readout status={status} />
        </>
      ) : null}
    </PanelSection>
  );
}
