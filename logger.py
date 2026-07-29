from datetime import datetime


class Logger:

    def __init__(self, filename="vinted.log"):
        self.filename = filename

    def log(self, message):

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        line = f"[{now}] {message}"

        print(line)

        with open(self.filename, "a", encoding="utf-8") as f:
            f.write(line + "\n")