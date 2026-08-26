// stripe listen --forward-to localhost:3001/api/webhooks/stripe

import { NextResponse } from "next/server";
import Stripe from "stripe";
import { env } from "~/env";
import { db } from "~/server/db";

const stripe = new Stripe(env.STRIPE_SECRET_KEY, {
  apiVersion: "2025-04-30.basil",
});

const webhookSecret = env.STRIPE_WEBHOOK_SECRET;

export async function POST(req: Request) {
  try {
    const body = await req.text();
    const signature = req.headers.get("stripe-signature") ?? "";

    let event: Stripe.Event;

    try {
      event = stripe.webhooks.constructEvent(body, signature, webhookSecret);
    } catch (error) {
      console.error("Webhook signature verification failed", error);
      return new NextResponse("Webhook signature verification failed", {
        status: 400,
      });
    }

    if (event.type === "checkout.session.completed") {
      const session = event.data.object;
      if (session.payment_status !== "paid") {
        return new NextResponse(null, { status: 200 });
      }

      const customerId =
        typeof session.customer === "string" ? session.customer : null;
      if (!customerId) {
        throw new Error("Paid checkout session has no Stripe customer ID");
      }

      const retrievedSession = await stripe.checkout.sessions.retrieve(
        session.id,
        { expand: ["line_items"] },
      );

      const priceId = retrievedSession.line_items?.data[0]?.price?.id;
      const creditsByPrice = new Map([
        [env.STRIPE_SMALL_CREDIT_PACK, 50],
        [env.STRIPE_MEDIUM_CREDIT_PACK, 150],
        [env.STRIPE_LARGE_CREDIT_PACK, 500],
      ]);
      const creditsToAdd = priceId ? creditsByPrice.get(priceId) : undefined;

      if (!creditsToAdd) {
        console.error("Ignoring checkout with an unknown credit-pack price", {
          eventId: event.id,
          priceId,
        });
        return new NextResponse(null, { status: 200 });
      }

      try {
        await db.$transaction(async (tx) => {
          await tx.stripeWebhookEvent.create({
            data: { id: event.id },
          });
          await tx.user.update({
            where: { stripeCustomerId: customerId },
            data: { credits: { increment: creditsToAdd } },
          });
        });
      } catch (error) {
        if ((error as { code?: string }).code === "P2002") {
          return new NextResponse(null, { status: 200 });
        }
        throw error;
      }
    }

    return new NextResponse(null, { status: 200 });
  } catch (error) {
    console.error("Error processing webhook:", error);
    return new NextResponse("Webhook error", { status: 500 });
  }
}
