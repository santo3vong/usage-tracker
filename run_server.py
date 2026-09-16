"""Run the canonical local Usage Tracker on port 5050."""

import server


if __name__ == "__main__":
    server.PORT = 5050
    server.run_server()
