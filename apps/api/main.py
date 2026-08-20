import os
import logging
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.middleware.cors import CORSMiddleware

from mcp.server import MCPServer
from supabase import create_client, Client

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("strata")

# Load environment variables (from local directory or root)
load_dotenv()
if not os.getenv("SUPABASE_URL"):
    load_dotenv("../../.env")

SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")
DEFAULT_USER_ID: str = os.getenv("DEFAULT_USER_ID", "")

# Cached client singletons and defaults
_supabase_client: Optional[Client] = None
_cached_user_id: Optional[str] = None

GLOBAL_CONVERSATION_TITLE = "STRATA Global Memory"


def get_supabase() -> Optional[Client]:
    """Retrieve or initialize the Supabase client singleton."""
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client

    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.warning("Supabase credentials not configured (SUPABASE_URL or SUPABASE_KEY is missing)")
        return None

    try:
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Connected to Supabase at %s", SUPABASE_URL)
        return _supabase_client
    except Exception as e:
        logger.error("Failed to initialize Supabase client: %s", e)
        return None


def get_system_user_id(db: Client) -> Optional[str]:
    """Resolves a valid user ID for conversations table foreign key constraint."""
    global _cached_user_id
    if _cached_user_id:
        return _cached_user_id

    if DEFAULT_USER_ID:
        _cached_user_id = DEFAULT_USER_ID
        return _cached_user_id

    try:
        # Fetch first available user from auth
        users_resp = db.auth.admin.list_users()
        if users_resp and len(users_resp) > 0:
            _cached_user_id = str(users_resp[0].id)
            logger.info("Using system user ID: %s", _cached_user_id)
            return _cached_user_id
    except Exception as e:
        logger.warning("Could not automatically query auth.users: %s", e)

    return None


async def _get_or_create_memory_conversation(db: Client, title: str = GLOBAL_CONVERSATION_TITLE) -> Optional[str]:
    """Finds an existing memory conversation or creates a new one."""
    try:
        # Check if conversation already exists
        existing = db.table("conversations").select("id").eq("title", title).limit(1).execute()
        if existing.data and len(existing.data) > 0:
            return existing.data[0]["id"]

        # Ensure valid user_id for foreign key constraint
        user_id = get_system_user_id(db)
        insert_payload: Dict[str, Any] = {"title": title}
        if user_id:
            insert_payload["user_id"] = user_id

        created = db.table("conversations").insert(insert_payload).execute()
        if created.data and len(created.data) > 0:
            conv_id = created.data[0]["id"]
            logger.info("Created new context conversation '%s' with ID: %s", title, conv_id)
            return conv_id
    except Exception as e:
        logger.error("Error ensuring memory conversation: %s", e)
    return None


# Initialize Modern MCP Server
mcp = MCPServer(
    name="strata",
    title="STRATA — Universal Context Bridge",
    description="Persistent contextual memory and knowledge bridge for LLMs and AI agents",
    version="2.0.0",
)


@mcp.tool(
    name="save_memory",
    description="Save a snippet of information, decision, preference, code snippet, or context into persistent memory."
)
async def save_memory(content: str, title: Optional[str] = None, tags: Optional[str] = None) -> str:
    """Save content into long-term context memory."""
    if not content or not content.strip():
        return "Error: Content cannot be empty."

    db = get_supabase()
    if not db:
        return "Error: Supabase database is not configured. Please check SUPABASE_URL and SUPABASE_KEY."

    try:
        conv_title = title if title else GLOBAL_CONVERSATION_TITLE
        conv_id = await _get_or_create_memory_conversation(db, conv_title)

        if not conv_id:
            return "Error: Unable to locate or create a conversation container for memory storage."

        formatted_content = content.strip()
        if tags and tags.strip():
            formatted_content = f"[{tags.strip()}]\n{formatted_content}"

        message_data: Dict[str, Any] = {
            "conversation_id": conv_id,
            "role": "system",
            "content": formatted_content,
            "model_used": "mcp-strata",
        }

        res = db.table("messages").insert(message_data).execute()
        if res.data and len(res.data) > 0:
            msg_id = res.data[0].get("id", "unknown")
            preview = formatted_content[:60] + "..." if len(formatted_content) > 60 else formatted_content
            logger.info("Saved memory [%s]: %s", msg_id, preview)
            return f"Successfully saved memory (ID: {msg_id}): \"{preview}\""
        return "Warning: Memory insert request executed but no record was returned."
    except Exception as e:
        logger.error("Error saving memory: %s", e)
        return f"Error saving memory: {str(e)}"


@mcp.tool(
    name="search_memory",
    description="Search persistent memory for relevant past context, discussions, code snippets, or user preferences."
)
async def search_memory(query: str, limit: int = 5) -> str:
    """Search stored context memories matching the query."""
    if not query or not query.strip():
        return "Error: Search query cannot be empty."

    db = get_supabase()
    if not db:
        return "Error: Supabase database is not configured."

    try:
        search_term = query.strip()
        # Search messages with ilike query
        response = (
            db.table("messages")
            .select("id, content, role, model_used, created_at")
            .ilike("content", f"%{search_term}%")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )

        records = response.data or []

        # If no exact text matches, fetch most recent context entries as fallback
        if not records:
            recent_res = (
                db.table("messages")
                .select("id, content, role, model_used, created_at")
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            recent_data = recent_res.data or []
            if not recent_data:
                return f"No memories found matching '{search_term}', and no recent context exists in storage."

            formatted = "\n\n".join(
                [f"• [{r.get('created_at', '')[:19].replace('T', ' ')}] (ID: {r.get('id')})\n  {r.get('content')}" for r in recent_data]
            )
            return f"No exact matches for '{search_term}'. Showing {len(recent_data)} most recent memories:\n\n{formatted}"

        formatted = "\n\n".join(
            [f"• [{r.get('created_at', '')[:19].replace('T', ' ')}] (ID: {r.get('id')})\n  {r.get('content')}" for r in records]
        )
        return f"Found {len(records)} memories matching '{search_term}':\n\n{formatted}"

    except Exception as e:
        logger.error("Error searching memory: %s", e)
        return f"Error searching memory: {str(e)}"


@mcp.tool(
    name="list_memories",
    description="List the most recently stored memories and context entries."
)
async def list_memories(limit: int = 10) -> str:
    """List recent memories from the persistent store."""
    db = get_supabase()
    if not db:
        return "Error: Supabase database is not configured."

    try:
        response = (
            db.table("messages")
            .select("id, content, created_at, model_used")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )

        records = response.data or []
        if not records:
            return "No stored memories found in STRATA database."

        lines = [f"Total retrieved: {len(records)} recent memory entries:\n"]
        for idx, r in enumerate(records, 1):
            created = r.get("created_at", "")[:19].replace("T", " ")
            lines.append(f"{idx}. [{created}] (ID: {r.get('id')})\n   {r.get('content')}")

        return "\n".join(lines)
    except Exception as e:
        logger.error("Error listing memories: %s", e)
        return f"Error listing memories: {str(e)}"


@mcp.tool(
    name="delete_memory",
    description="Delete a specific memory by its ID."
)
async def delete_memory(memory_id: str) -> str:
    """Delete a memory record by its UUID."""
    if not memory_id or not memory_id.strip():
        return "Error: Memory ID must be provided."

    db = get_supabase()
    if not db:
        return "Error: Supabase database is not configured."

    try:
        res = db.table("messages").delete().eq("id", memory_id.strip()).execute()
        if res.data and len(res.data) > 0:
            return f"Successfully deleted memory with ID {memory_id}."
        return f"No memory record found with ID {memory_id} to delete."
    except Exception as e:
        logger.error("Error deleting memory: %s", e)
        return f"Error deleting memory: {str(e)}"


@mcp.tool(
    name="get_stats",
    description="Get status and statistics about the STRATA memory store."
)
async def get_stats() -> str:
    """Get system and memory statistics."""
    db = get_supabase()
    db_connected = db is not None

    msg_count = 0
    conv_count = 0

    if db:
        try:
            m_res = db.table("messages").select("id", count="exact").execute()
            msg_count = m_res.count if m_res.count is not None else len(m_res.data or [])

            c_res = db.table("conversations").select("id", count="exact").execute()
            conv_count = c_res.count if c_res.count is not None else len(c_res.data or [])
        except Exception as e:
            logger.warning("Could not calculate exact counts: %s", e)

    return (
        f"STRATA Universal Context Bridge v2.0.0\n"
        f"• Database Status: {'Connected' if db_connected else 'Disconnected'}\n"
        f"• Supabase URL: {SUPABASE_URL if SUPABASE_URL else 'Not Configured'}\n"
        f"• Total Stored Memories: {msg_count}\n"
        f"• Total Memory Containers: {conv_count}\n"
        f"• MCP Transport: SSE (/sse, /messages/)"
    )


# Build Starlette application with SSE transport
app = mcp.sse_app(sse_path="/sse", message_path="/messages/")

# Health check / status endpoints
async def health_check(request):
    """Health check endpoint."""
    return JSONResponse({
        "status": "healthy",
        "service": "strata-mcp-server",
        "version": "2.0.0",
        "database": "connected" if get_supabase() is not None else "disconnected"
    })

app.routes.append(Route("/", endpoint=health_check, methods=["GET"]))
app.routes.append(Route("/health", endpoint=health_check, methods=["GET"]))

# Add CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting STRATA MCP Server on http://0.0.0.0:8000")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
