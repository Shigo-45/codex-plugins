from gemini_baton.config import Settings
from gemini_baton.writing import WritingService
import argparse
import time


class FakeTransport:
    def cli(self, job, model, effort="high"):
        time.sleep(0.3)
        return {"text": "writing worker survived", "actual_model_version": model,
                "requested_effort": effort, "effective_effort": effort, "usage": {}}

    def api(self, job, model, effort="high"):
        time.sleep(0.3)
        return {"text": "writing worker survived", "actual_model_version": model + "-test",
                "requested_effort": effort, "effective_effort": effort, "usage": {}}

    def aistudio(self, job, model, effort="high"):
        time.sleep(0.3)
        return {"text": "writing worker survived", "actual_model_version": model,
                "requested_effort": effort, "effective_effort": effort, "usage": {}}

    def web(self, job, model, effort="high"):
        raise AssertionError("An earlier route succeeded")


parser = argparse.ArgumentParser()
parser.add_argument("--job-id", required=True)
args = parser.parse_args()
WritingService(Settings.from_env()).execute(args.job_id, transport=FakeTransport())
