from app.core.config import get_setting


class Engine:
    def __init__(self, store):
        self.store = store
        self.queue = []

    def run(self, jobs, retries=3, strict=False):
        done = []
        for job in jobs:
            if job is None:
                continue
            if job.get("kind") == "order":
                if job.get("total", 0) <= 0:
                    if strict:
                        raise ValueError("empty order")
                    continue
                for attempt in range(retries):
                    try:
                        if self.store.save(job):
                            done.append(job)
                            break
                        elif attempt == retries - 1:
                            self.queue.append(job)
                    except IOError:
                        if attempt == retries - 1:
                            raise
            elif job.get("kind") == "refund":
                if job.get("amount", 0) > 0 and job.get("order"):
                    done.append(job)
                elif strict:
                    raise ValueError("bad refund")
            elif job.get("kind") == "report":
                while self.queue:
                    pending = self.queue.pop()
                    if pending.get("total"):
                        done.append(pending)
            else:
                if get_setting("SHOP_DEBUG"):
                    print("unknown job", job)
        return done

    def drain(self):
        out, self.queue = self.queue, []
        return out
