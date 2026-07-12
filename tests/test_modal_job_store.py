from unittest.mock import patch


def _run_mutator(status, _job_id, mutator):
    mutator(status)
    return status


def test_record_step_1_call_does_not_reopen_completed_dispatch():
    import modal_job_store

    status = {"step_1_spawn_status": "completed", "step_1_modal_call_id": None}
    with patch("modal_job_store.update_status", side_effect=lambda job_id, mutator: _run_mutator(status, job_id, mutator)), \
         patch("modal_job_store.append_log"):
        modal_job_store.record_step_1_call("job-1", "call-1")

    assert status["step_1_spawn_status"] == "completed"
    assert status["step_1_modal_call_id"] == "call-1"


def test_record_step_3_call_does_not_reopen_failed_dispatch():
    import modal_job_store

    status = {"step_3_spawn_status": "failed", "step_3_modal_call_id": None}
    with patch("modal_job_store.update_status", side_effect=lambda job_id, mutator: _run_mutator(status, job_id, mutator)), \
         patch("modal_job_store.append_log"):
        modal_job_store.record_step_3_call("job-1", "call-3")

    assert status["step_3_spawn_status"] == "failed"
    assert status["step_3_modal_call_id"] == "call-3"
