import http.server
import socketserver
import subprocess
import json
import threading
import queue
import time
import signal
import atexit
import sys
import logging
import argparse

# Configuration
PORT = 8000
STDIO_SERVER_COMMAND = ['uv', '--directory', 'D:/Projects/Telegram-MCP-Server-Integration', 'run', 'main.py']
REQUEST_TIMEOUT = 60
COMMON_HEADERS = {
    'Content-type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type'
}

# Global references
global_server = None
verbose_logging = False
logger = logging.getLogger(__name__)

def setup_logging(verbose=False):
    """Setup logging based on verbosity level."""
    global verbose_logging
    verbose_logging = verbose
    
    if verbose:
        # Info mode: detailed logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            force=True
        )
    else:
        # Silence mode: minimal logging
        logging.basicConfig(
            level=logging.WARNING,
            format='%(levelname)s: %(message)s',
            force=True
        )

def log_info(message):
    """Log info message only in verbose mode."""
    if verbose_logging:
        logger.info(message)

def log_error(message):
    """Always log error messages."""
    logger.error(message)

def log_warning(message):
    """Always log warning messages."""
    logger.warning(message)

def read_json_response(stdout_queue, timeout):
    """Read lines from stdout queue until we get a complete valid JSON object."""
    accumulated_text = ""
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        try:
            line = stdout_queue.get(timeout=1)
            accumulated_text += line
            
            try:
                clean_text = accumulated_text.strip()
                if clean_text:
                    response_json = json.loads(clean_text)
                    log_info(f"Successfully parsed JSON response: {str(response_json)[:200]}...")
                    return response_json
            except json.JSONDecodeError:
                continue
                
        except queue.Empty:
            continue
    
    if accumulated_text.strip():
        log_error(f"Timeout reached with incomplete JSON: {accumulated_text[:200]}...")
        raise TimeoutError(f"Timeout waiting for complete JSON response. Got: {accumulated_text[:100]}...")
    else:
        raise TimeoutError("No response from STDIO server within timeout period")

def cleanup_process(server):
    """Clean up the STDIO process and associated resources."""
    if hasattr(server, 'stdio_process') and server.stdio_process:
        log_info("Cleaning up STDIO process...")
        try:
            if server.stdio_process.poll() is None:
                server.stdio_process.terminate()
                try:
                    server.stdio_process.wait(timeout=5)
                    log_info("STDIO process terminated gracefully")
                except subprocess.TimeoutExpired:
                    log_warning("STDIO process didn't terminate gracefully, killing...")
                    server.stdio_process.kill()
                    server.stdio_process.wait()
                    log_info("STDIO process killed")
        except Exception as e:
            log_error(f"Error during cleanup: {e}")

def send_http_response(handler, status_code, data=None, is_json=True):
    """Helper to send HTTP responses with common headers."""
    handler.send_response(status_code)
    for header, value in COMMON_HEADERS.items():
        handler.send_header(header, value)
    handler.end_headers()
    
    if data is not None:
        if is_json and not isinstance(data, bytes):
            data = json.dumps(data).encode('utf-8')
        elif isinstance(data, str):
            data = data.encode('utf-8')
        handler.wfile.write(data)

def clear_stale_responses(queue_obj):
    """Clear any stale responses from the queue."""
    cleared_count = 0
    try:
        while not queue_obj.empty():
            stale_response = queue_obj.get_nowait()
            log_warning(f"Cleared stale response: {stale_response[:100]}...")
            cleared_count += 1
    except queue.Empty:
        pass
    return cleared_count

class StdioProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        """Override to control HTTP access logging based on verbosity."""
        if verbose_logging:
            super().log_message(format, *args)
    
    def _ensure_stdio_process(self):
        """Ensure STDIO server process is running."""
        if not hasattr(self.server, 'stdio_process') or self.server.stdio_process.poll() is not None:
            log_info("Starting new STDIO server process...")
            self.server.stdio_process = subprocess.Popen(
                STDIO_SERVER_COMMAND,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )
            
            self.server.stdout_queue = queue.Queue()
            self.server.stdout_thread = threading.Thread(
                target=lambda: [self.server.stdout_queue.put(line) for line in iter(self.server.stdio_process.stdout.readline, '')],
                daemon=True
            )
            self.server.stdout_thread.start()
            time.sleep(0.5)
            log_info("STDIO server process started")

    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        request_json = json.loads(post_data.decode('utf-8'))
        is_notification = 'id' not in request_json
        
        self._ensure_stdio_process()
        
        if not hasattr(self.server, 'request_lock'):
            self.server.request_lock = threading.Lock()

        try:
            with self.server.request_lock:
                request_type = "notification" if is_notification else "request"
                log_info(f"Received MCP {request_type}: {json.dumps(request_json)[:200]}...")
                
                clear_stale_responses(self.server.stdout_queue)
                
                self.server.stdio_process.stdin.write(json.dumps(request_json) + '\n')
                self.server.stdio_process.stdin.flush()
                log_info("Request sent to STDIO server")

                if self.server.stdio_process.poll() is not None:
                    stderr_output = self.server.stdio_process.stderr.read()
                    raise RuntimeError(f"STDIO server exited unexpectedly. Stderr: {stderr_output}")

                if is_notification:
                    log_info("Processing notification - no response expected")
                    send_http_response(self, 200, b'', is_json=False)
                    log_info("Notification processed successfully")
                else:
                    log_info("Processing request - waiting for response...")
                    response_json = read_json_response(self.server.stdout_queue, REQUEST_TIMEOUT)
                    send_http_response(self, 200, response_json)
                    log_info("Response sent to client successfully")

        except Exception as e:
            log_error(f"Error processing MCP request: {e}")
            error_message = {'error': str(e), 'details': 'Failed to communicate with STDIO server'}
            send_http_response(self, 500, error_message)
            log_error(f"Error response sent: {error_message}")
            
            if hasattr(self.server, 'stdio_process') and self.server.stdio_process.poll() is None:
                log_warning("Restarting STDIO process due to error...")
                self.server.stdio_process.terminate()
                self.server.stdio_process.wait()

    def do_GET(self):
        send_http_response(self, 200, {'status': 'MCP STDIO Proxy running', 'port': PORT})

class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description='MCP STDIO Proxy Server')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('-s', '--silence', action='store_true', default=True,
                      help='Minimal logging (default)')
    group.add_argument('-i', '--info', action='store_true', default=False,
                      help='Detailed logging for debugging')
    return parser.parse_args()

def main():
    """Main function to start the proxy server."""
    args = parse_arguments()
    verbose = args.info
    
    setup_logging(verbose)
    
    # Setup signal handlers and cleanup
    signal.signal(signal.SIGINT, lambda s, f: (cleanup_process(global_server), sys.exit(0)))
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, lambda s, f: (cleanup_process(global_server), sys.exit(0)))
    atexit.register(lambda: cleanup_process(global_server) if global_server else None)
    
    try:
        global global_server
        with ThreadingHTTPServer(("", PORT), StdioProxyHandler) as httpd:
            global_server = httpd
            
            # Startup messages (always shown)
            print(f"MCP STDIO Proxy Server starting on port {PORT}")
            print(f"Proxying to: {' '.join(STDIO_SERVER_COMMAND)}")
            if verbose:
                print(f"Verbose logging enabled")
                print(f"Request timeout: {REQUEST_TIMEOUT} seconds")
            print("Press Ctrl+C to stop")
            
            httpd.serve_forever()
            
    except KeyboardInterrupt:
        print("\nServer stopped by user")
    except Exception as e:
        log_error(f"Server error: {e}")
        sys.exit(1)
    finally:
        if global_server:
            cleanup_process(global_server)

if __name__ == '__main__':
    main()
