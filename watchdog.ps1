# Watchdog for claude-deepseek-proxy
# Checks proxy health every 5s, restarts if dead or stuck.
param(
    [int]$Port = 9194,
    [string]$ProxyScript = "C:\Users\fhc\Desktop\kache\claude_deepseek_proxy.py"
)

$ErrorActionPreference = "SilentlyContinue"

while ($true) {
    $alive = $false
    try {
        # Test: SSL connect + send minimal request
        $body = '{"model":"deepseek-chat","max_tokens":8,"messages":[{"role":"user","content":"x"}],"thinking":{"type":"enabled","budget_tokens":1000},"stream":false}'
        $req = [System.Net.HttpWebRequest]::Create("https://127.0.0.1:$Port/v1/messages")
        $req.Method = "POST"
        $req.ContentType = "application/json"
        $req.Timeout = 5000
        $req.Headers["x-api-key"] = $env:DEEPSEEK_API_KEY
        $req.Headers["anthropic-version"] = "2023-06-01"
        $req.ServerCertificateValidationCallback = { $true }
        
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
        $req.ContentLength = $bytes.Length
        $stream = $req.GetRequestStream()
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Close()
        
        $resp = $req.GetResponse()
        $resp.Close()
        $alive = $true
    } catch {
        $alive = $false
    }
    
    if (-not $alive) {
        $ts = Get-Date -Format "HH:mm:ss"
        Write-Host "[$ts] WATCHDOG: proxy dead, restarting..." -ForegroundColor Yellow
        Get-Process python -ErrorAction SilentlyContinue | 
            Where-Object { $_.MainWindowTitle -eq "" } |
            Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep 1
        Start-Process python -ArgumentList $ProxyScript, $Port -WindowStyle Hidden
        Start-Sleep 3
    }
    
    Start-Sleep 5
}
