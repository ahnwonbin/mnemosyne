"""Idempotent cloud delivery with exact per-event acknowledgment validation."""
import json
from urllib.parse import urlsplit, quote
import httpx
from bridge.outbox import get_default_device_id

def validate_endpoint(url):
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError("invalid endpoint URL")
    if parsed.scheme != "https" and not (parsed.scheme=="http" and parsed.hostname in {"localhost","127.0.0.1","::1"}):
        raise ValueError("use HTTPS or loopback HTTP through a private tunnel")
    return url.rstrip("/")


def refresh_context_cache(cache, outbox, endpoint_url=None, transport=None):
    """Refresh local projects with the separate read credential; retain offline data on failure."""
    token = cache.get_read_token()
    if not token:
        return {"refreshed": 0, "errors": [], "skipped": "read token missing"}
    endpoint = validate_endpoint(endpoint_url or cache.get_endpoint())
    connection = outbox._get_connection()
    try:
        project_ids = [row[0] for row in connection.execute("SELECT DISTINCT project_id FROM outbox_events")]
    finally:
        connection.close()
    result = {"refreshed": 0, "errors": []}
    with httpx.Client(timeout=15, transport=transport, follow_redirects=False) as client:
        for project_id in project_ids:
            try:
                response = client.get(f"{endpoint}/api/v1/projects/{quote(project_id, safe='')}/context",
                                      params={"max_tokens": 800}, headers={"Authorization": f"Bearer {token}"})
                response.raise_for_status()
                context = response.json()
                if not isinstance(context, dict) or context.get("project_id") != project_id:
                    raise ValueError("context project mismatch")
                cache.save_cached_context(project_id, context)
                result["refreshed"] += 1
            except Exception as exc:
                result["errors"].append({"project_id": project_id, "error": type(exc).__name__})
    return result

class SyncResult:
    def __init__(self,success,batch_size=0,accepted=0,duplicate=0,rejected=0,error=None):
        self.success,self.batch_size=success,batch_size
        self.accepted,self.duplicate,self.rejected=accepted,duplicate,rejected
        self.error=error
    def to_dict(self):
        return vars(self).copy()

class SyncClient:
    def __init__(self,endpoint_url,auth_token,outbox,device_id=None,timeout=15.0,transport=None):
        self.endpoint_url=validate_endpoint(endpoint_url)
        if not auth_token:
            raise ValueError("a device token is required")
        self.auth_token,self.outbox=auth_token,outbox
        self.device_id=device_id or get_default_device_id()
        self.timeout,self.transport=timeout,transport

    def sync_batch(self,batch_size=50):
        events=self.outbox.get_pending_batch(limit=batch_size)
        ids=[e.event_id for e in events]
        payload={"device_id":self.device_id,"events":[e.to_transmission_dict() for e in events],
                 "reported_backlog":self.outbox.get_stats()["pending"]+len(events)}
        # Bound wire size without dropping claimed events.
        while len(json.dumps(payload,ensure_ascii=False).encode()) > 1900000 and len(events)>1:
            excess=events.pop()
            self.outbox.release_in_flight([excess.event_id],"deferred to next bounded batch")
            payload["events"].pop()
        ids=[e.event_id for e in events]
        try:
            if any(e.device_id != self.device_id for e in events):
                raise ValueError("outbox/device mismatch")
            with httpx.Client(timeout=self.timeout,transport=self.transport,follow_redirects=False) as client:
                response=client.post(self.endpoint_url+"/api/v1/events/batch",json=payload,
                    headers={"Authorization":"Bearer "+self.auth_token})
            if response.status_code!=200:
                raise RuntimeError("HTTP "+str(response.status_code))
            acks=response.json().get("acknowledgments")
            if not isinstance(acks,list) or any(not isinstance(a,dict) for a in acks):
                raise ValueError("invalid acknowledgments")
            ack_ids=[a.get("event_id") for a in acks]
            if len(set(ack_ids))!=len(ack_ids) or set(ack_ids)!=set(ids):
                raise ValueError("incomplete or unrelated acknowledgment IDs")
            if any(a.get("status") not in {"accepted","duplicate","rejected"} for a in acks):
                raise ValueError("unknown acknowledgment status")
            self.outbox.acknowledge_results(acks)
            counts={s:sum(a["status"]==s for a in acks) for s in ("accepted","duplicate","rejected")}
            return SyncResult(success=True,batch_size=len(events),**counts)
        except Exception as exc:
            # Avoid echoing server bodies or network URLs containing credentials.
            error=str(exc) if isinstance(exc,(ValueError,RuntimeError)) else type(exc).__name__
            self.outbox.release_in_flight(ids,error)
            return SyncResult(False,batch_size=len(events),error=error)

    def sync_all(self,batch_size=50,max_batches=50):
        result={"total_batches":0,"total_accepted":0,"total_duplicate":0,"total_rejected":0,"last_error":None}
        for _ in range(max_batches):
            res=self.sync_batch(batch_size)
            if res.batch_size:
                result["total_batches"]+=1
            for kind in ("accepted","duplicate","rejected"):
                result["total_"+kind]+=getattr(res,kind)
            if not res.success:
                result["last_error"]=res.error
                break
            if not res.batch_size:
                break
        result["outbox_stats"]=self.outbox.get_stats()
        return result
