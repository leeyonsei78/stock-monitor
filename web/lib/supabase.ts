import { createClient } from "@supabase/supabase-js";

const url  = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!;

// 브라우저용 싱글턴
let _client: ReturnType<typeof createClient> | null = null;

export function getSupabase() {
  if (!_client) _client = createClient(url, anonKey);
  return _client;
}
