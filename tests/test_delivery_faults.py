
import httpx
import pytest

from bridge.outbox import Outbox
from bridge.sync import SyncClient

def enqueue(box,event_id=None,payload=None):
    return box.enqueue("dev","project","codex","session","prompt_submit",payload or {"prompt":"hello"},
                       "source:1",event_id=event_id)

def test_expired_claim_recovers_after_process_death(tmp_path):
    path=tmp_path/"outbox.db"
    box=Outbox(path)
    e=enqueue(box)
    assert len(box.get_pending_batch(lease_seconds=-1))==1
    restarted=Outbox(path)
    recovered=restarted.get_pending_batch()
    assert [r.event_id for r in recovered]==[e.event_id]
    assert restarted.get_pending_batch()==[]

def test_repeated_native_event_does_not_increment_sequence(tmp_path):
    box=Outbox(tmp_path/"outbox.db")
    a=enqueue(box,event_id="stable")
    b=enqueue(box,event_id="stable")
    assert a.event_id==b.event_id
    assert a.session_sequence==b.session_sequence==1
    assert enqueue(box).session_sequence==2
    with pytest.raises(ValueError,match="different content"):
        enqueue(box,event_id="stable",payload={"prompt":"changed"})
    assert box.get_stats()["total"]==2

@pytest.mark.parametrize("response",[
    {"acknowledgments":[]},
    {"acknowledgments":[{"event_id":"unrelated","status":"accepted"}]},
    {"acknowledgments":None},
    {"acknowledgments":"accepted"},
])
def test_malformed_success_never_loses_queue(tmp_path,response):
    box=Outbox(tmp_path/"outbox.db")
    enqueue(box)
    sync=SyncClient("https://example.test","fixture",box,"dev",
                    transport=httpx.MockTransport(lambda r:httpx.Response(200,json=response)))
    assert not sync.sync_batch().success
    assert box.get_stats()["pending"]==1
    assert box.get_stats()["in_flight"]==0

def test_network_outage_never_permanently_quarantines(tmp_path):
    box=Outbox(tmp_path/"outbox.db")
    e=enqueue(box)
    for i in range(25):
        box.retry(e.event_id)
        box.get_pending_batch()
        box.release_in_flight([e.event_id],"offline")
    assert box.get_stats()["pending"]==1
    assert box.get_stats()["quarantined"]==0

def test_partial_ack_retries_without_false_success(tmp_path):
    box=Outbox(tmp_path/"outbox.db")
    a,_b=enqueue(box),enqueue(box)
    sync=SyncClient("https://example.test","fixture",box,"dev",
          transport=httpx.MockTransport(lambda r:httpx.Response(200,json={"acknowledgments":[{"event_id":a.event_id,"status":"accepted"}]})))
    assert not sync.sync_batch().success
    assert box.get_stats()["pending"]==2

def test_transport_and_capacity_limits(tmp_path):
    box=Outbox(tmp_path/"outbox.db",max_pending_bytes=20)
    with pytest.raises(OSError,match="capacity"):
        enqueue(box,payload={"prompt":"a"*50})
    with pytest.raises(ValueError,match="HTTPS"):
        SyncClient("http://public.example","secret",box,"dev")
