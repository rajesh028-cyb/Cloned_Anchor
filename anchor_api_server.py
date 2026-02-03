# ANCHOR API Server - Flask wrapper for Mock Scammer API integration
# Production-ready HTTP server for external API calls

"""
ANCHOR API Server
=================
Flask-based HTTP server for integrating ANCHOR with external services.

Endpoints:
- POST /process     - Process scammer message, return structured response
- POST /reset       - Reset session
- GET  /summary     - Get session summary
- GET  /health      - Health check

Usage:
    python anchor_api_server.py
    curl -X POST http://localhost:5000/process -H "Content-Type: application/json" -d '{"message": "Hello"}'
"""

import logging

try:
    from flask import Flask, request, jsonify
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False

from anchor_agent import AnchorAgent, create_agent
from memory import get_memory_manager

# Global agent instances (keyed by session_id)
_agents = {}


def get_or_create_agent(session_id: str = "default") -> AnchorAgent:
    """Get or create agent for session"""
    if session_id not in _agents:
        _agents[session_id] = create_agent(session_id)
    return _agents[session_id]


if FLASK_AVAILABLE:
    app = Flask(__name__)
    
    # Suppress Flask's default request logging for clean output
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    
    @app.route('/health', methods=['GET'])
    def health():
        """Health check endpoint"""
        return jsonify({
            "status": "healthy",
            "service": "ANCHOR API",
            "version": "2.0.0",
            "mode": "api-driven"
        })
    
    @app.route('/process', methods=['POST'])
    def process():
        """
        Process scammer message.
        
        Request body:
        {
            "message": "<scammer text>",
            "session_id": "<optional session id>"
        }
        
        Response:
        {
            "response": "<persona reply>",
            "state": "<current state>",
            "extracted_artifacts": {...},
            "conversation_log": [...],
            "engagement_turn": <int>,
            "session_id": "<session_id>",
            "metadata": {...}
        }
        """
        try:
            data = request.get_json()
            
            if not data:
                return jsonify({"error": "No JSON body provided"}), 400
            
            session_id = data.get("session_id", "default")
            agent = get_or_create_agent(session_id)
            
            # Process message
            result = agent.process_api_message(data)
            
            # Minimal server-side logging
            print(f"Received message | State: {result['state']}")
            
            return jsonify(result)
            
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    @app.route('/reset', methods=['POST'])
    def reset():
        """
        Reset session.
        
        Request body:
        {
            "session_id": "<optional session id>"
        }
        """
        try:
            data = request.get_json() or {}
            session_id = data.get("session_id", "default")
            
            if session_id in _agents:
                _agents[session_id].reset_session()
                return jsonify({"status": "reset", "session_id": session_id})
            else:
                return jsonify({"status": "no_session", "session_id": session_id})
                
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    @app.route('/summary', methods=['GET'])
    def summary():
        """
        Get session summary.
        
        Query params:
        - session_id: Session ID (default: "default")
        """
        try:
            session_id = request.args.get("session_id", "default")
            
            if session_id in _agents:
                result = _agents[session_id].get_session_summary()
                return jsonify(result)
            else:
                return jsonify({"error": "Session not found"}), 404
                
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    @app.route('/sessions', methods=['GET'])
    def list_sessions():
        """List all active sessions"""
        return jsonify({
            "sessions": list(_agents.keys()),
            "count": len(_agents)
        })


def run_server(host: str = "0.0.0.0", port: int = 5000, debug: bool = False):
    """
    Run ANCHOR API server.
    
    Args:
        host: Bind address
        port: Port number
        debug: Enable debug mode
    """
    if not FLASK_AVAILABLE:
        print("ERROR: Flask not installed. Run: pip install flask")
        return
    
    print(f"ANCHOR API Server running on http://{host}:{port}")
    print("POST /process - Process scammer message")
    
    app.run(host=host, port=port, debug=debug, use_reloader=False)


if __name__ == "__main__":
    import sys
    
    port = 5000
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    
    run_server(port=port, debug=False)
