# Revenue OS v1.1 -- prove server-side revocation with an OLD bearer token.
# Manual QA: credentials entered interactively, never printed or stored.
# Only loopback HTTP is allowed. No database mutation beyond login/logout.
param(
    [ValidateSet('127.0.0.1', 'localhost')][string]$HostName = '127.0.0.1',
    [ValidateRange(1, 65535)][int]$Port = 8000
)
$ErrorActionPreference = 'Stop'
$baseUrl = 'http://' + $HostName + ':' + $Port + '/api/v1'
$token = $null
$pass = $null
$otp = $null
$loginOk = $false
$revokeOk = $false

function Unprotect-Input([System.Security.SecureString]$Value) {
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

try {
    $username = Read-Host 'Usuario (admin o comercial de PRUEBA)'
    if ([string]::IsNullOrWhiteSpace($username)) { throw 'Usuario vacío' }
    $pass = Read-Host 'Contraseña' -AsSecureString
    $otp = Read-Host 'Código MFA (intro vacío si es un comercial)' -AsSecureString
    $payload = @{
        username = $username.Trim()
        password = Unprotect-Input $pass
    }
    $otpValue = Unprotect-Input $otp
    if (-not [string]::IsNullOrWhiteSpace($otpValue)) { $payload.otp = $otpValue.Trim() }
    $otpValue = $null
    $body = ConvertTo-Json -InputObject $payload -Compress
    $payload = $null
    $login = Invoke-RestMethod -Uri "$baseUrl/auth/login" -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 10
    $body = $null
    $loginOk = $true
    $token = $login.access_token
    if ($token -notmatch '^[a-f0-9]{64}$') { throw 'Token de sesión inesperado' }

    $headers = @{ Authorization = "Bearer $token" }
    $before = Invoke-RestMethod -Uri "$baseUrl/me" -Headers $headers -TimeoutSec 10
    if ($before.username -ne $username.Trim()) { throw 'Identidad distinta de la solicitada' }
    Write-Host 'PASS: el token autenticaba antes del logout.'

    $result = Invoke-RestMethod -Uri "$baseUrl/auth/session" -Method Delete -Headers $headers -TimeoutSec 10
    if ($result.revoked -ne $true) { throw 'El servidor no confirmó la revocación' }
    $revokeOk = $true
    Write-Host 'PASS: DELETE /auth/session confirmó revoked=true.'

    $rejected = $false
    try {
        $null = Invoke-WebRequest -Uri "$baseUrl/me" -Method Get -Headers $headers -TimeoutSec 10 -UseBasicParsing -ErrorAction Stop
    } catch {
        $response = $_.Exception.Response
        if ($null -eq $response) { throw 'No hubo respuesta HTTP al reintentar el token antiguo' }
        if ([int]$response.StatusCode -eq 401) {
            $rejected = $true
        } else {
            throw "El token antiguo devolvió HTTP $([int]$response.StatusCode), esperado 401"
        }
    }
    if (-not $rejected) { throw 'FALLO DE SEGURIDAD: el token antiguo aún funciona' }
    Write-Host 'PASS: el token ANTIGUO devuelve HTTP 401 tras logout.'
    Write-Host 'PASS: revocación de sesión verificada en servidor (sin revelar credenciales).'
} finally {
    if ($loginOk -and -not $revokeOk -and $token) {
        Write-Warning 'No se ha confirmado la revocación. Reintenta con conexión estable y revisa la sesión desde administración.'
    }
    $token = $null
    $headers = $null
    $pass = $null
    $otp = $null
    $body = $null
    $payload = $null
}
