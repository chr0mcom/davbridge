#!/bin/bash
# Checks EWS streaming through the reverse proxy: shows the arrival time of every chunk.
# Usage: stream_probe.sh --url https://dav.example.com --user test-user --password-stdin [--http1.1|--http2] [--minutes N]
# The password (app password of a test account) is read as one line from stdin, never as an argument.
set -u
proto="--http1.1"; minutes=1; url=""; user=""; pw_stdin=0
usage() { echo "Usage: $0 --url <base URL> --user <user> --password-stdin [--http1.1|--http2] [--minutes N]" >&2; exit 64; }
while [ $# -gt 0 ]; do
  case "$1" in
    --http1.1|--http2) proto="$1" ;;
    --url) url="${2:-}"; shift ;;
    --user) user="${2:-}"; shift ;;
    --minutes) minutes="${2:-}"; shift ;;
    --password-stdin) pw_stdin=1 ;;
    *) usage ;;
  esac
  shift
done
[ -n "$url" ] && [ -n "$user" ] && [ "$pw_stdin" = 1 ] || usage
case "$minutes" in ''|*[!0-9]*) usage ;; esac
IFS= read -r pw || { echo "No password on stdin" >&2; exit 64; }
url="${url%/}"
cred() { local u="$user:$pw"; u="${u//\\/\\\\}"; u="${u//\"/\\\"}"; printf 'user = "%s"\n' "$u"; }
env='<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages" xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"><soap:Header><t:RequestServerVersion Version="Exchange2016"/></soap:Header><soap:Body>%s</soap:Body></soap:Envelope>'
sub='<m:Subscribe><m:StreamingSubscriptionRequest><t:FolderIds><t:DistinguishedFolderId Id="calendar"/></t:FolderIds><t:EventTypes><t:EventType>CreatedEvent</t:EventType><t:EventType>ModifiedEvent</t:EventType><t:EventType>DeletedEvent</t:EventType></t:EventTypes></m:StreamingSubscriptionRequest></m:Subscribe>'
subscription="$(printf "$env" "$sub" | curl -sS -m 30 "$proto" -K <(cred) -H 'Content-Type: text/xml; charset=utf-8' --data-binary @- "$url/EWS/Exchange.asmx" | sed -n 's/.*<m:SubscriptionId>\([^<]*\)<.*/\1/p')"
[ -n "$subscription" ] || { echo "Subscribe failed"; exit 2; }
echo "Subscription created ($proto), streaming for $minutes min; start $(date +%T)"
get="<m:GetStreamingEvents><m:SubscriptionIds><t:SubscriptionId>$subscription</t:SubscriptionId></m:SubscriptionIds><m:ConnectionTimeout>$minutes</m:ConnectionTimeout></m:GetStreamingEvents>"
start=$(date +%s)
printf "$env" "$get" | curl -sS -N -m $((minutes * 60 + 90)) "$proto" -K <(cred) -H 'Content-Type: text/xml; charset=utf-8' --data-binary @- "$url/EWS/Exchange.asmx" \
  -w '\ncurl_exit_http=%{http_code} duration_s=%{time_total} http_version=%{http_version}\n' \
  | sed -u 's/<?xml[^>]*>//; s/<s:Envelope/\n<s:Envelope/g' \
  | awk -v s="$start" '/^<s:Envelope/ { n++; t=systime()-s; clock=strftime("%T") } /Envelope/ { st="?"; if (match($0, /ConnectionStatus>[A-Za-z]+</)) st=substr($0, RSTART+17, RLENGTH-18); if ($0 ~ /Notification>/) st=st"+Notification"; printf "%s +%ds chunk %d: %s\n", clock, t, n, st; fflush(); next } /curl_exit_http/ { print; fflush() }'
