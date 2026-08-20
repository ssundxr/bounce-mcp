-- =============================================================================
-- STRATA — UNIVERSAL CONTEXT BRIDGE DATABASE SCHEMA
-- PostgreSQL + pgvector + Supabase Auth & Storage
-- =============================================================================

-- Enable required PostgreSQL extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. CONVERSATIONS TABLE
-- Stores conversation containers / memory workspaces
CREATE TABLE IF NOT EXISTS public.conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT 'STRATA Memory Context',
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. MESSAGES TABLE
-- Stores context memories, prompts, assistant responses, and code snippets
CREATE TABLE IF NOT EXISTS public.messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES public.conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    content TEXT NOT NULL,
    model_used TEXT DEFAULT 'mcp-strata',
    tags TEXT[] DEFAULT '{}',
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 3. CONTEXT VECTORS TABLE (For Vector DB / Semantic RAG)
CREATE TABLE IF NOT EXISTS public.context_vectors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES public.conversations(id) ON DELETE CASCADE,
    message_id UUID REFERENCES public.messages(id) ON DELETE CASCADE,
    embedding VECTOR(1536), -- 1536 dims for standard embedding models (e.g. OpenAI text-embedding-3-small)
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 4. ROW LEVEL SECURITY (RLS)
ALTER TABLE public.conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.context_vectors ENABLE ROW LEVEL SECURITY;

-- Conversations RLS Policies
CREATE POLICY "Users can manage their own conversations" 
ON public.conversations FOR ALL 
USING (auth.uid() = user_id OR auth.uid() IS NULL);

-- Messages RLS Policies
CREATE POLICY "Users can access messages of their conversations" 
ON public.messages FOR ALL 
USING (
    EXISTS (
        SELECT 1 FROM public.conversations c 
        WHERE c.id = messages.conversation_id 
        AND (c.user_id = auth.uid() OR auth.uid() IS NULL)
    )
);

-- Context Vectors RLS Policies
CREATE POLICY "Users can access context vectors of their conversations" 
ON public.context_vectors FOR ALL 
USING (
    EXISTS (
        SELECT 1 FROM public.conversations c 
        WHERE c.id = context_vectors.conversation_id 
        AND (c.user_id = auth.uid() OR auth.uid() IS NULL)
    )
);

-- 5. PERFORMANCE INDEXES
-- Index for conversation lookups
CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON public.conversations(user_id);
CREATE INDEX IF NOT EXISTS idx_conversations_title ON public.conversations(title);

-- Index for fetching messages by conversation and date
CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON public.messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON public.messages(created_at DESC);

-- Full-Text Search GIN index on messages content
CREATE INDEX IF NOT EXISTS idx_messages_content_gin ON public.messages USING gin(to_tsvector('english', content));

-- HNSW Vector Index for fast semantic similarity search (cosine distance)
CREATE INDEX IF NOT EXISTS idx_context_vectors_embedding_hnsw 
ON public.context_vectors USING hnsw (embedding vector_cosine_ops);

-- 6. AUTO-UPDATE TRIGGER FOR conversations.updated_at
CREATE OR REPLACE FUNCTION public.handle_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS set_conversations_updated_at ON public.conversations;
CREATE TRIGGER set_conversations_updated_at
BEFORE UPDATE ON public.conversations
FOR EACH ROW
EXECUTE FUNCTION public.handle_updated_at();
