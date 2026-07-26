using System;
using System.Collections.Generic;
using System.Runtime.InteropServices.WindowsRuntime;
using System.Text;
using Windows.Media.Control;

// Windows' global media transport controls - the same mechanism behind the
// play/pause keys on a keyboard. Every well-behaved player registers here:
// Spotify's desktop app, Apple Music, Cider, and YouTube Music playing in a
// browser tab. That makes it the one control surface that works across
// services without an API key, a developer agreement, or a paid tier.
//
// What it can do: read what's playing (title, artist, album, position,
// duration), and play/pause/skip/seek.
// What it cannot do: start a named playlist, read the queue, or set volume.
// Those need a per-service API - which is exactly why this is a fallback
// provider rather than a replacement for one.
//
// Returns JSON strings so the PowerShell and Python callers don't need to
// know anything about WinRT.
public static class MediaSessions
{
    private static GlobalSystemMediaTransportControlsSessionManager Manager()
    {
        return GlobalSystemMediaTransportControlsSessionManager.RequestAsync()
            .AsTask().GetAwaiter().GetResult();
    }

    private static string Esc(string s)
    {
        if (string.IsNullOrEmpty(s)) return "";
        var sb = new StringBuilder(s.Length + 8);
        foreach (char c in s)
        {
            switch (c)
            {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                    else sb.Append(c);
                    break;
            }
        }
        return sb.ToString();
    }

    private static GlobalSystemMediaTransportControlsSession Find(string appId)
    {
        var manager = Manager();
        if (string.IsNullOrEmpty(appId)) return manager.GetCurrentSession();
        foreach (var s in manager.GetSessions())
        {
            if (string.Equals(s.SourceAppUserModelId, appId, StringComparison.OrdinalIgnoreCase))
                return s;
        }
        return null;
    }

    /// <summary>Every app currently registered as a media source.</summary>
    public static string List()
    {
        var manager = Manager();
        var current = manager.GetCurrentSession();
        string currentId = current == null ? "" : current.SourceAppUserModelId;

        var parts = new List<string>();
        foreach (var s in manager.GetSessions())
        {
            string status;
            try { status = s.GetPlaybackInfo().PlaybackStatus.ToString(); }
            catch { status = "Unknown"; }
            parts.Add("{\"app\":\"" + Esc(s.SourceAppUserModelId) + "\",\"status\":\""
                      + Esc(status) + "\",\"current\":"
                      + (s.SourceAppUserModelId == currentId ? "true" : "false") + "}");
        }
        return "[" + string.Join(",", parts.ToArray()) + "]";
    }

    /// <summary>What a session is playing. Empty appId means the active session.</summary>
    public static string State(string appId)
    {
        var session = Find(appId);
        if (session == null) return "{\"ok\":false,\"error\":\"no media session\"}";

        string title = "", artist = "", album = "";
        try
        {
            var props = session.TryGetMediaPropertiesAsync().AsTask().GetAwaiter().GetResult();
            if (props != null)
            {
                title = props.Title ?? "";
                artist = props.Artist ?? "";
                album = props.AlbumTitle ?? "";
            }
        }
        catch { }

        double elapsed = 0, duration = 0;
        try
        {
            var t = session.GetTimelineProperties();
            elapsed = t.Position.TotalSeconds;
            duration = t.EndTime.TotalSeconds;
        }
        catch { }

        bool playing = false, canPause = false, canNext = false, canPrev = false, canSeek = false;
        try
        {
            var info = session.GetPlaybackInfo();
            playing = info.PlaybackStatus ==
                      GlobalSystemMediaTransportControlsSessionPlaybackStatus.Playing;
            var c = info.Controls;
            canPause = c.IsPauseEnabled;
            canNext = c.IsNextEnabled;
            canPrev = c.IsPreviousEnabled;
            canSeek = c.IsPlaybackPositionEnabled;
        }
        catch { }

        return "{\"ok\":true"
             + ",\"app\":\"" + Esc(session.SourceAppUserModelId) + "\""
             + ",\"track\":\"" + Esc(title) + "\""
             + ",\"artist\":\"" + Esc(artist) + "\""
             + ",\"album\":\"" + Esc(album) + "\""
             + ",\"playing\":" + (playing ? "true" : "false")
             + ",\"elapsed\":" + elapsed.ToString("0.###", System.Globalization.CultureInfo.InvariantCulture)
             + ",\"duration\":" + duration.ToString("0.###", System.Globalization.CultureInfo.InvariantCulture)
             + ",\"can_pause\":" + (canPause ? "true" : "false")
             + ",\"can_next\":" + (canNext ? "true" : "false")
             + ",\"can_previous\":" + (canPrev ? "true" : "false")
             + ",\"can_seek\":" + (canSeek ? "true" : "false")
             + "}";
    }

    /// <summary>play | pause | playpause | next | previous | seek:&lt;seconds&gt;</summary>
    public static string Control(string appId, string action)
    {
        var session = Find(appId);
        if (session == null) return "{\"ok\":false,\"error\":\"no media session\"}";

        try
        {
            bool ok;
            action = (action ?? "").ToLowerInvariant();
            if (action.StartsWith("seek:"))
            {
                double seconds = double.Parse(action.Substring(5),
                    System.Globalization.CultureInfo.InvariantCulture);
                // TryChangePlaybackPositionAsync takes 100-nanosecond ticks.
                // No digit separators: this is built with the .NET Framework
                // C# 5 compiler, where 10_000_000 is a syntax error.
                ok = session.TryChangePlaybackPositionAsync((long)(seconds * 10000000))
                        .AsTask().GetAwaiter().GetResult();
            }
            else
            {
                switch (action)
                {
                    case "play": ok = session.TryPlayAsync().AsTask().GetAwaiter().GetResult(); break;
                    case "pause": ok = session.TryPauseAsync().AsTask().GetAwaiter().GetResult(); break;
                    case "playpause": ok = session.TryTogglePlayPauseAsync().AsTask().GetAwaiter().GetResult(); break;
                    case "next": ok = session.TrySkipNextAsync().AsTask().GetAwaiter().GetResult(); break;
                    case "previous": ok = session.TrySkipPreviousAsync().AsTask().GetAwaiter().GetResult(); break;
                    default: return "{\"ok\":false,\"error\":\"unknown action\"}";
                }
            }
            return "{\"ok\":" + (ok ? "true" : "false") + "}";
        }
        catch (Exception ex)
        {
            return "{\"ok\":false,\"error\":\"" + Esc(ex.GetType().Name) + "\"}";
        }
    }
}
