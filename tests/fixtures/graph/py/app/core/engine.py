from app.util import clamp


class Engine:
    def run(self, items):
        total = 0
        for item in items:
            if item > 10:
                if item % 2:
                    total += clamp(item)
                else:
                    total -= 1
            elif item < 0:
                total = 0
        return self.finish(total)

    def finish(self, total):
        return total
