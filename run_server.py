"""Run the public Usage Tracker checkout on its local preview port."""

import server


if __name__ == "__main__":
    server.PORT = 5051
    server.run_server()
