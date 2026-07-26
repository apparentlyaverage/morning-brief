using System;
using System.Runtime.InteropServices;

// Reads and sets the Windows master output volume via Core Audio.
//
// PowerShell has no built-in way to set the system volume - the usual trick
// is sending volume-up/down keystrokes, which moves in coarse 2% steps and
// can't land on an exact value. These COM interfaces set it precisely.
//
// The interface method declarations below must stay in exact vtable order
// even for the methods we never call - the runtime dispatches by slot index,
// so deleting an unused entry silently calls the wrong function.
public static class SystemVolume
{
    [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
    private class MMDeviceEnumerator { }

    [Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMMDeviceEnumerator
    {
        int EnumAudioEndpoints(int dataFlow, int stateMask, out IntPtr devices);
        int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice endpoint);
    }

    [Guid("D666063F-1587-4E43-81F1-B948E807363F"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMMDevice
    {
        int Activate(ref Guid iid, int clsCtx, IntPtr activationParams,
                     [MarshalAs(UnmanagedType.IUnknown)] out object iface);
    }

    [Guid("5CDF2C82-841E-4546-9722-0CF74078229A"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IAudioEndpointVolume
    {
        int RegisterControlChangeNotify(IntPtr notify);
        int UnregisterControlChangeNotify(IntPtr notify);
        int GetChannelCount(out int count);
        int SetMasterVolumeLevel(float levelDb, ref Guid eventContext);
        int SetMasterVolumeLevelScalar(float level, ref Guid eventContext);
        int GetMasterVolumeLevel(out float levelDb);
        int GetMasterVolumeLevelScalar(out float level);
        int SetChannelVolumeLevel(uint channel, float levelDb, ref Guid eventContext);
        int SetChannelVolumeLevelScalar(uint channel, float level, ref Guid eventContext);
        int GetChannelVolumeLevel(uint channel, out float levelDb);
        int GetChannelVolumeLevelScalar(uint channel, out float level);
        int SetMute([MarshalAs(UnmanagedType.Bool)] bool mute, ref Guid eventContext);
        int GetMute([MarshalAs(UnmanagedType.Bool)] out bool mute);
    }

    private const int ERender = 0;      // eRender  - output devices
    private const int EMultimedia = 1;  // eMultimedia role
    private const int ClsCtxAll = 23;

    private static IAudioEndpointVolume GetEndpoint()
    {
        var enumerator = (IMMDeviceEnumerator)(new MMDeviceEnumerator());
        IMMDevice device;
        Marshal.ThrowExceptionForHR(
            enumerator.GetDefaultAudioEndpoint(ERender, EMultimedia, out device));

        var iid = typeof(IAudioEndpointVolume).GUID;
        object iface;
        Marshal.ThrowExceptionForHR(device.Activate(ref iid, ClsCtxAll, IntPtr.Zero, out iface));
        return (IAudioEndpointVolume)iface;
    }

    /// <summary>Master output volume, 0.0 - 1.0.</summary>
    public static float Get()
    {
        float level;
        Marshal.ThrowExceptionForHR(GetEndpoint().GetMasterVolumeLevelScalar(out level));
        return level;
    }

    /// <summary>Set master output volume, 0.0 - 1.0. Also unmutes.</summary>
    public static void Set(float level)
    {
        if (level < 0f) level = 0f;
        if (level > 1f) level = 1f;
        var endpoint = GetEndpoint();
        var ctx = Guid.Empty;
        Marshal.ThrowExceptionForHR(endpoint.SetMasterVolumeLevelScalar(level, ref ctx));
        // A muted machine at 30% is still a silent briefing.
        Marshal.ThrowExceptionForHR(endpoint.SetMute(false, ref ctx));
    }

    public static bool IsMuted()
    {
        bool muted;
        Marshal.ThrowExceptionForHR(GetEndpoint().GetMute(out muted));
        return muted;
    }
}
