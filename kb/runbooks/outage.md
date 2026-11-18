<!-- source: claude -->
# Runbook: something is out

Run `netdiag_triage` (optionally `service=<hostname>`). It stops at the first failing layer.

## STOP at 1: no default route
This device has no network. Check Wi-Fi or cable, then `netdiag_interfaces`.

## STOP at 2: gateway not answering
The router or the local link is down. Power-cycle the router; check the switch. Nothing past the router can work until this is fixed.

## STOP at 3: DNS failing
- "internet works (1.1.1.1 answers)": the local DNS server is the problem. Restart the router's DNS (or the AdGuard/Pi-hole container). As a stopgap, use 1.1.1.1 on the affected device.
- "1.1.1.1 does not answer either": the internet is probably down; see STOP at 4.

## STOP at 4: no route to the internet
ISP or upstream outage. Loyd switches to its local brain automatically (offline detection). Services that only need the house keep working once split DNS is in place. Check the ISP status page from a phone on cellular. If outages repeat, cellular failover on the router is the fix.

## Step 5 says "(public)" for one of our services
The name resolves to Cloudflare from inside the house: split DNS isn't set up for it, so it will fail when the internet does.

## Everything up but an app is broken
`ops_containers`, then `ops_container_logs name=<container>`; `ops_gpu_status` for vision or model problems.
