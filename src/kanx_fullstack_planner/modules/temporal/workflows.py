from temporalio import activity, workflow


@activity.defn
async def smoke_activity() -> str:
    return "ok-workflow"


@workflow.defn
class SmokeWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            smoke_activity, start_to_close_timeout=__import__("datetime").timedelta(seconds=10)
        )
