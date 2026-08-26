"use server";

import { redirect } from "next/navigation";
import Stripe from "stripe";
import { env } from "~/env";
import { auth } from "~/server/auth";
import { db } from "~/server/db";

const stripe = new Stripe(env.STRIPE_SECRET_KEY, {
  apiVersion: "2025-04-30.basil",
});

export type PriceId = "small" | "medium" | "large";

const PRICE_IDS: Record<PriceId, string> = {
  small: env.STRIPE_SMALL_CREDIT_PACK,
  medium: env.STRIPE_MEDIUM_CREDIT_PACK,
  large: env.STRIPE_LARGE_CREDIT_PACK,
};

export async function createCheckoutSession(priceId: PriceId) {
  const serverSession = await auth();
  if (!serverSession?.user?.id) {
    throw new Error("Unauthorized");
  }

  const selectedPrice = PRICE_IDS[priceId];
  if (!selectedPrice) {
    throw new Error("Unknown credit pack");
  }

  const user = await db.user.findUniqueOrThrow({
    where: {
      id: serverSession.user.id,
    },
    select: { stripeCustomerId: true, email: true },
  });

  let stripeCustomerId = user.stripeCustomerId;
  if (!stripeCustomerId) {
    const customer = await stripe.customers.create({ email: user.email });
    stripeCustomerId = customer.id;
    await db.user.update({
      where: { id: serverSession.user.id },
      data: { stripeCustomerId },
    });
  }

  const session = await stripe.checkout.sessions.create({
    line_items: [{ price: selectedPrice, quantity: 1 }],
    customer: stripeCustomerId,
    mode: "payment",
    success_url: `${env.BASE_URL}/dashboard?success=true`,
    cancel_url: `${env.BASE_URL}/dashboard/billing?canceled=true`,
  });

  if (!session.url) {
    throw new Error("Failed to create session URL");
  }

  redirect(session.url);
}
