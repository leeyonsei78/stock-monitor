import { NextRequest, NextResponse } from "next/server";

const API_BASE = process.env.API_BASE_URL ?? "";
const API_KEY  = process.env.API_SECRET    ?? "";

export async function POST(req: NextRequest) {
  if (!API_BASE) {
    return NextResponse.json({ error: "API_BASE_URL not configured" }, { status: 503 });
  }

  const body = await req.json();
  const { side, ...rest } = body;

  if (side !== "buy" && side !== "sell") {
    return NextResponse.json({ error: "side must be buy or sell" }, { status: 400 });
  }

  try {
    const res = await fetch(`${API_BASE}/api/trade/${side}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-api-key": API_KEY,
      },
      body: JSON.stringify(rest),
    });

    const data = await res.json();
    if (!res.ok) {
      return NextResponse.json({ error: data.detail ?? "주문 실패" }, { status: res.status });
    }
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json({ error: "Oracle VM 연결 실패" }, { status: 502 });
  }
}
