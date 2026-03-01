using System;
using System.Runtime.InteropServices;
using System.Text;
using System.Collections.Generic;
using System.Threading;
using System.IO;

public class UsbPrinterIO {
    // --- SetupDi for device enumeration ---
    static readonly Guid GUID_DEVINTERFACE_USBPRINT = new Guid(
        0x28d78fad, 0x5a12, 0x11d1,
        0xae, 0x5b, 0x00, 0x00, 0xf8, 0x03, 0xa8, 0xc2);

    const int DIGCF_PRESENT = 0x02;
    const int DIGCF_DEVICEINTERFACE = 0x10;

    [StructLayout(LayoutKind.Sequential)]
    struct SP_DEVICE_INTERFACE_DATA {
        public int cbSize;
        public Guid InterfaceClassGuid;
        public int Flags;
        public IntPtr Reserved;
    }

    [DllImport("setupapi.dll", CharSet=CharSet.Auto, SetLastError=true)]
    static extern IntPtr SetupDiGetClassDevs(
        ref Guid ClassGuid, IntPtr Enumerator, IntPtr hwndParent, int Flags);

    [DllImport("setupapi.dll", SetLastError=true)]
    static extern bool SetupDiEnumDeviceInterfaces(
        IntPtr DeviceInfoSet, IntPtr DeviceInfoData,
        ref Guid InterfaceClassGuid, int MemberIndex,
        ref SP_DEVICE_INTERFACE_DATA DeviceInterfaceData);

    [DllImport("setupapi.dll", CharSet=CharSet.Auto, SetLastError=true)]
    static extern bool SetupDiGetDeviceInterfaceDetail(
        IntPtr DeviceInfoSet, ref SP_DEVICE_INTERFACE_DATA DeviceInterfaceData,
        IntPtr DeviceInterfaceDetailData, int DeviceInterfaceDetailDataSize,
        ref int RequiredSize, IntPtr DeviceInfoData);

    [DllImport("setupapi.dll", SetLastError=true)]
    static extern bool SetupDiDestroyDeviceInfoList(IntPtr DeviceInfoSet);

    // --- CreateFile for direct I/O ---
    [DllImport("kernel32.dll", CharSet=CharSet.Auto, SetLastError=true)]
    static extern IntPtr CreateFile(
        string lpFileName, uint dwDesiredAccess, uint dwShareMode,
        IntPtr lpSecurityAttributes, uint dwCreationDisposition,
        uint dwFlagsAndAttributes, IntPtr hTemplateFile);

    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool WriteFile(
        IntPtr hFile, byte[] lpBuffer, int nNumberOfBytesToWrite,
        out int lpNumberOfBytesWritten, IntPtr lpOverlapped);

    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool ReadFile(
        IntPtr hFile, byte[] lpBuffer, int nNumberOfBytesToRead,
        out int lpNumberOfBytesRead, IntPtr lpOverlapped);

    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool CloseHandle(IntPtr hObject);

    const uint GENERIC_READ = 0x80000000;
    const uint GENERIC_WRITE = 0x40000000;
    const uint FILE_SHARE_READ = 0x01;
    const uint FILE_SHARE_WRITE = 0x02;
    const uint OPEN_ALWAYS = 4;
    const uint FILE_ATTRIBUTE_NORMAL = 0x80;
    static readonly IntPtr INVALID_HANDLE = new IntPtr(-1);

    static string Win32ErrorDescription(int code) {
        switch (code) {
            case 2: return "Device not found (ERROR_FILE_NOT_FOUND)";
            case 5: return "Access denied — another app may be using the printer (ERROR_ACCESS_DENIED)";
            case 21: return "Device not ready — printer may be off or disconnected (ERROR_NOT_READY)";
            case 31: return "Device not functioning — try unplugging and re-plugging USB (ERROR_GEN_FAILURE)";
            case 32: return "Device in use by another process (ERROR_SHARING_VIOLATION)";
            case 87: return "Invalid parameter — device path may be stale (ERROR_INVALID_PARAMETER)";
            case 121: return "Timeout waiting for printer response (ERROR_SEM_TIMEOUT)";
            case 1167: return "Device not connected (ERROR_DEVICE_NOT_CONNECTED)";
            case 1168: return "Device path not found — printer may have been unplugged (ERROR_NOT_FOUND)";
            default: return "Win32 error " + code;
        }
    }

    // --- Device Discovery ---

    public static string FindBrotherPrinter() {
        Guid guid = GUID_DEVINTERFACE_USBPRINT;
        IntPtr hDevInfo = SetupDiGetClassDevs(
            ref guid, IntPtr.Zero, IntPtr.Zero,
            DIGCF_PRESENT | DIGCF_DEVICEINTERFACE);

        if (hDevInfo == IntPtr.Zero || hDevInfo == INVALID_HANDLE)
            return "ERROR:SetupDiGetClassDevs failed: " + Marshal.GetLastWin32Error();

        try {
            int idx = 0;
            while (true) {
                SP_DEVICE_INTERFACE_DATA ifData = new SP_DEVICE_INTERFACE_DATA();
                ifData.cbSize = Marshal.SizeOf(typeof(SP_DEVICE_INTERFACE_DATA));
                if (!SetupDiEnumDeviceInterfaces(hDevInfo, IntPtr.Zero, ref guid, idx, ref ifData))
                    break;

                int requiredSize = 0;
                SetupDiGetDeviceInterfaceDetail(hDevInfo, ref ifData, IntPtr.Zero, 0, ref requiredSize, IntPtr.Zero);

                IntPtr detailData = Marshal.AllocHGlobal(requiredSize);
                try {
                    Marshal.WriteInt32(detailData, IntPtr.Size == 8 ? 8 : 6);
                    if (SetupDiGetDeviceInterfaceDetail(hDevInfo, ref ifData, detailData, requiredSize, ref requiredSize, IntPtr.Zero)) {
                        string path = Marshal.PtrToStringAuto(new IntPtr(detailData.ToInt64() + 4));
                        if (path.ToLower().Contains("vid_04f9")) return path;
                    }
                } finally {
                    Marshal.FreeHGlobal(detailData);
                }
                idx++;
            }
        } finally {
            SetupDiDestroyDeviceInfoList(hDevInfo);
        }
        return "ERROR:No Brother USB printer found";
    }

    // --- Helper: open device handle ---

    static IntPtr OpenDevice(string devicePath) {
        return CreateFile(
            devicePath,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            IntPtr.Zero,
            OPEN_ALWAYS,
            FILE_ATTRIBUTE_NORMAL,
            IntPtr.Zero);
    }

    // --- Operations that open their own handle (stateless) ---

    public static string OpenSendReceive(string devicePath, string dataFilePath, int readWaitMs) {
        IntPtr h = OpenDevice(devicePath);
        if (h == IntPtr.Zero || h == INVALID_HANDLE)
            return "ERROR:CreateFile failed: " + Win32ErrorDescription(Marshal.GetLastWin32Error());

        try {
            byte[] data = File.ReadAllBytes(dataFilePath);
            int written;
            if (!WriteFile(h, data, data.Length, out written, IntPtr.Zero))
                return "ERROR:WriteFile failed: " + Win32ErrorDescription(Marshal.GetLastWin32Error());

            Thread.Sleep(readWaitMs > 0 ? readWaitMs : 1000);
            byte[] buf = new byte[16384];
            int bytesRead;
            ReadFile(h, buf, buf.Length, out bytesRead, IntPtr.Zero);
            if (bytesRead > 0) {
                return "DATA:" + Encoding.UTF8.GetString(buf, 0, bytesRead);
            }
            return "NODATA:written=" + written;
        } finally {
            CloseHandle(h);
        }
    }

    public static string OpenAndSend(string devicePath, string dataFilePath) {
        IntPtr h = OpenDevice(devicePath);
        if (h == IntPtr.Zero || h == INVALID_HANDLE)
            return "ERROR:CreateFile failed: " + Win32ErrorDescription(Marshal.GetLastWin32Error());

        try {
            byte[] data = File.ReadAllBytes(dataFilePath);
            int written;
            if (!WriteFile(h, data, data.Length, out written, IntPtr.Zero))
                return "ERROR:WriteFile failed: " + Win32ErrorDescription(Marshal.GetLastWin32Error());
            return "OK:" + written;
        } finally {
            CloseHandle(h);
        }
    }

    public static string OpenAndRead(string devicePath, int waitMs) {
        IntPtr h = OpenDevice(devicePath);
        if (h == IntPtr.Zero || h == INVALID_HANDLE)
            return "ERROR:CreateFile failed: " + Win32ErrorDescription(Marshal.GetLastWin32Error());

        try {
            Thread.Sleep(waitMs > 0 ? waitMs : 1000);
            byte[] buf = new byte[16384];
            int bytesRead;
            ReadFile(h, buf, buf.Length, out bytesRead, IntPtr.Zero);
            if (bytesRead > 0) {
                return "DATA:" + Encoding.UTF8.GetString(buf, 0, bytesRead);
            }
            return "NODATA:err=" + Marshal.GetLastWin32Error();
        } finally {
            CloseHandle(h);
        }
    }

    // --- Thread-safe read with timeout (ReadFile blocks on USB) ---

    static string ReadWithTimeout(IntPtr h, int timeoutMs) {
        string result = null;
        int bytesRead = 0;
        byte[] buf = new byte[16384];

        Thread readThread = new Thread(() => {
            ReadFile(h, buf, buf.Length, out bytesRead, IntPtr.Zero);
            if (bytesRead > 0) {
                result = Encoding.UTF8.GetString(buf, 0, bytesRead);
            }
        });
        readThread.IsBackground = true;
        readThread.Start();

        if (!readThread.Join(timeoutMs)) {
            // Timeout - cancel the blocking read
            try {
                CancelIoEx(h, IntPtr.Zero);
                readThread.Join(1000);
            } catch {}
            return null;
        }
        return result;
    }

    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool CancelIoEx(IntPtr hFile, IntPtr lpOverlapped);

    // --- Print a single label on an open handle ---

    static string PrintOneLabel(IntPtr h, string printXml, byte[] jpegData, StringBuilder sb, string label) {
        byte[] xmlBytes = Encoding.UTF8.GetBytes(printXml);
        int written;
        if (!WriteFile(h, xmlBytes, xmlBytes.Length, out written, IntPtr.Zero)) {
            return "ERROR:WriteFile(xml) failed: " + Win32ErrorDescription(Marshal.GetLastWin32Error());
        }
        sb.AppendLine(label + "XML_SENT:" + written);

        // Wait for "ready to receive" - retry up to 30s
        string cmdResponse = null;
        for (int attempt = 0; attempt < 6; attempt++) {
            cmdResponse = ReadWithTimeout(h, 5000);
            if (cmdResponse != null) break;
        }
        if (cmdResponse == null) {
            sb.AppendLine(label + "CMD_RESPONSE:timeout");
            return "ERROR:Printer not responding";
        }
        bool cmdOk = cmdResponse.Contains("<code>0</code>");
        sb.AppendLine(label + "CMD_RESPONSE:" + (cmdOk ? "ready" : cmdResponse.Replace("\n", " ").Trim()));
        if (!cmdOk) {
            return "ERROR:Printer rejected command";
        }

        // Send JPEG data
        if (!WriteFile(h, jpegData, jpegData.Length, out written, IntPtr.Zero)) {
            return "ERROR:WriteFile(jpeg) failed: " + Win32ErrorDescription(Marshal.GetLastWin32Error());
        }
        sb.AppendLine(label + "JPEG_SENT:" + written);

        // Wait for "data received" acknowledgment
        string dataResp = ReadWithTimeout(h, 15000);
        if (dataResp != null) {
            sb.AppendLine(label + "DATA:" + (dataResp.Contains("<code>0</code>") ? "received" : dataResp.Replace("\n", " ").Trim()));
        }

        // Wait for print/cut completion
        string compResp = ReadWithTimeout(h, 15000);
        if (compResp != null) {
            sb.AppendLine(label + "COMPLETE:" + compResp.Replace("\n", " ").Trim().Substring(0, Math.Min(100, compResp.Length)));
        }

        return null; // success
    }

    // --- Single print job ---

    public static string PrintJob(string devicePath, string printXmlPath, string jpegPath) {
        var sb = new StringBuilder();
        IntPtr h = OpenDevice(devicePath);
        if (h == IntPtr.Zero || h == INVALID_HANDLE)
            return "ERROR:CreateFile failed: " + Win32ErrorDescription(Marshal.GetLastWin32Error());

        try {
            // Flush any pending data
            ReadWithTimeout(h, 1000);

            string printXml = File.ReadAllText(printXmlPath, Encoding.UTF8);
            byte[] jpegData = File.ReadAllBytes(jpegPath);
            string err = PrintOneLabel(h, printXml, jpegData, sb, "");
            if (err != null) return sb.ToString() + "\n" + err;
            sb.AppendLine("PRINT_OK");
            return sb.ToString();
        } finally {
            CloseHandle(h);
        }
    }

    // --- Batch print (multiple labels on single handle) ---
    // jsonPath points to a JSON file with: { "devicePath", "jobs": [{ "xmlPath", "jpegPath" }] }

    public static string BatchPrintJob(string devicePath, string[] xmlPaths, string[] jpegPaths) {
        var sb = new StringBuilder();
        IntPtr h = OpenDevice(devicePath);
        if (h == IntPtr.Zero || h == INVALID_HANDLE)
            return "ERROR:CreateFile failed: " + Win32ErrorDescription(Marshal.GetLastWin32Error());

        try {
            // Flush any pending data
            ReadWithTimeout(h, 1000);

            for (int i = 0; i < xmlPaths.Length; i++) {
                string label = "[" + (i + 1) + "/" + xmlPaths.Length + "] ";
                string printXml = File.ReadAllText(xmlPaths[i], Encoding.UTF8);
                byte[] jpegData = File.ReadAllBytes(jpegPaths[i]);
                string err = PrintOneLabel(h, printXml, jpegData, sb, label);
                if (err != null) return sb.ToString() + "\n" + err;
            }

            sb.AppendLine("BATCH_OK");
            return sb.ToString();
        } finally {
            CloseHandle(h);
        }
    }

    // --- Fire-and-forget print (no ReadFile, avoids USB hang) ---

    public static string PrintJobFireAndForget(string devicePath, string printXmlPath, string jpegPath) {
        var sb = new StringBuilder();
        IntPtr h = OpenDevice(devicePath);
        if (h == IntPtr.Zero || h == INVALID_HANDLE)
            return "ERROR:CreateFile failed: " + Win32ErrorDescription(Marshal.GetLastWin32Error());

        try {
            string printXml = File.ReadAllText(printXmlPath, Encoding.UTF8);
            byte[] xmlBytes = Encoding.UTF8.GetBytes(printXml);
            byte[] jpegData = File.ReadAllBytes(jpegPath);
            int written;

            if (!WriteFile(h, xmlBytes, xmlBytes.Length, out written, IntPtr.Zero)) {
                return "ERROR:WriteFile(xml) failed: " + Win32ErrorDescription(Marshal.GetLastWin32Error());
            }
            sb.AppendLine("XML_SENT:" + written);

            Thread.Sleep(300);

            if (!WriteFile(h, jpegData, jpegData.Length, out written, IntPtr.Zero)) {
                return "ERROR:WriteFile(jpeg) failed: " + Win32ErrorDescription(Marshal.GetLastWin32Error());
            }
            sb.AppendLine("JPEG_SENT:" + written);
            sb.AppendLine("PRINT_OK");
            return sb.ToString();
        } finally {
            CloseHandle(h);
        }
    }

    // --- XML helper ---

    static string ExtractXmlValue(string xml, string tag) {
        string open = "<" + tag + ">";
        string close = "</" + tag + ">";
        int start = xml.IndexOf(open);
        if (start < 0) return null;
        start += open.Length;
        int end = xml.IndexOf(close, start);
        if (end < 0) return null;
        return xml.Substring(start, end - start);
    }
}
