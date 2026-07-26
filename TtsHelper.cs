using System;
using System.IO;
using System.Collections.Generic;
using System.Runtime.InteropServices.WindowsRuntime;
using Windows.Media.SpeechSynthesis;

public static class TtsHelper
{
    public static string[] Voices()
    {
        var list = new List<string>();
        foreach (var v in SpeechSynthesizer.AllVoices) list.Add(v.DisplayName);
        return list.ToArray();
    }

    public static void ToWav(string text, string voiceMatch, double rate, string path)
    {
        var synth = new SpeechSynthesizer();
        if (!string.IsNullOrEmpty(voiceMatch))
        {
            foreach (var v in SpeechSynthesizer.AllVoices)
            {
                if (v.DisplayName.IndexOf(voiceMatch, StringComparison.OrdinalIgnoreCase) >= 0)
                { synth.Voice = v; break; }
            }
        }
        synth.Options.SpeakingRate = rate;
        var stream = synth.SynthesizeTextToStreamAsync(text).AsTask().GetAwaiter().GetResult();
        using (var fs = File.Create(path))
        using (var input = stream.AsStreamForRead())
            input.CopyTo(fs);
        synth.Dispose();
    }
}
