"use server";

import {
  GetObjectCommand,
  HeadObjectCommand,
  S3Client,
} from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";
import { revalidatePath } from "next/cache";
import { env } from "~/env";
import { inngest } from "~/inngest/client";
import { auth } from "~/server/auth";
import { db } from "~/server/db";

export async function processVideo(uploadedFileId: string) {
  const session = await auth();
  if (!session?.user?.id) throw new Error("Unauthorized");

  const uploadedVideo = await db.uploadedFile.findFirstOrThrow({
    where: {
      id: uploadedFileId,
      userId: session.user.id,
    },
    select: {
      uploaded: true,
      status: true,
      id: true,
      userId: true,
      s3Key: true,
    },
  });

  const retryableStatuses = ["failed", "no credits"];
  if (
    uploadedVideo.uploaded &&
    !retryableStatuses.includes(uploadedVideo.status)
  ) {
    return;
  }

  const s3Client = createS3Client();
  const uploadedObject = await s3Client.send(
    new HeadObjectCommand({
      Bucket: env.S3_BUCKET_NAME,
      Key: uploadedVideo.s3Key,
    }),
  );

  if (!uploadedObject.ContentLength) {
    throw new Error("Uploaded video is empty or missing");
  }

  const claimedUpload = uploadedVideo.uploaded
    ? await db.uploadedFile.updateMany({
        where: {
          id: uploadedVideo.id,
          userId: session.user.id,
          status: { in: retryableStatuses },
        },
        data: { status: "queued" },
      })
    : await db.uploadedFile.updateMany({
        where: {
          id: uploadedVideo.id,
          userId: session.user.id,
          uploaded: false,
        },
        data: { uploaded: true },
      });

  if (claimedUpload.count === 0) return;

  try {
    await inngest.send({
      name: "process-video-events",
      data: { uploadedFileId: uploadedVideo.id, userId: uploadedVideo.userId },
    });
  } catch (error) {
    await db.uploadedFile.update({
      where: { id: uploadedVideo.id },
      data: uploadedVideo.uploaded
        ? { status: uploadedVideo.status }
        : { uploaded: false },
    });
    throw error;
  }

  revalidatePath("/dashboard");
}

export async function ingestYouTubeVideo(youtubeUrl: string) {
  const session = await auth();
  if (!session?.user?.id) throw new Error("Unauthorized");

  const trimmedUrl = youtubeUrl.trim();
  if (!trimmedUrl) throw new Error("YouTube URL is required");

  // Validate YouTube URL
  const match = /(?:v=|\/|youtu\.be\/)([0-9A-Za-z_-]{11})/.exec(trimmedUrl);
  if (!match) {
    throw new Error("Invalid YouTube URL format. Please provide a valid YouTube watch link.");
  }
  const videoId = match[1]!;

  const user = await db.user.findUniqueOrThrow({
    where: { id: session.user.id },
    select: { credits: true },
  });

  if (user.credits <= 0) {
    throw new Error("You do not have enough credits to process this video");
  }

  const s3Key = `uploads/${videoId}/original.mp4`;

  // Create or update existing upload record
  const existing = await db.uploadedFile.findFirst({
    where: {
      s3Key,
      userId: session.user.id,
    },
  });

  let fileId: string;
  if (existing) {
    await db.uploadedFile.update({
      where: { id: existing.id },
      data: { status: "queued", uploaded: true },
    });
    fileId = existing.id;
  } else {
    const created = await db.uploadedFile.create({
      data: {
        s3Key,
        displayName: `YouTube Video (${videoId})`,
        uploaded: true,
        status: "queued",
        userId: session.user.id,
      },
    });
    fileId = created.id;
  }

  try {
    await inngest.send({
      name: "process-video-events",
      data: {
        uploadedFileId: fileId,
        userId: session.user.id,
        youtubeUrl: trimmedUrl,
      },
    });
  } catch (error) {
    console.error("Inngest dispatch failed:", error);
  }

  revalidatePath("/dashboard");
  return { success: true, uploadedFileId: fileId, videoId };
}

export async function getClipPlayUrl(
  clipId: string,
): Promise<{ success: boolean; url?: string; error?: string }> {
  const session = await auth();
  if (!session?.user?.id) {
    return { success: false, error: "Unauthorized" };
  }

  try {
    const clip = await db.clip.findUniqueOrThrow({
      where: {
        id: clipId,
        userId: session.user.id,
      },
    });

    const s3Client = createS3Client();

    const command = new GetObjectCommand({
      Bucket: env.S3_BUCKET_NAME,
      Key: clip.s3Key,
    });

    const signedUrl = await getSignedUrl(s3Client, command, {
      expiresIn: 3600,
    });

    return { success: true, url: signedUrl };
  } catch {
    return { success: false, error: "Failed to generate play URL." };
  }
}

function createS3Client() {
  return new S3Client({
    region: env.AWS_REGION,
    endpoint: env.AWS_ENDPOINT_URL_S3,
    forcePathStyle: Boolean(env.AWS_ENDPOINT_URL_S3),
    credentials: {
      accessKeyId: env.AWS_ACCESS_KEY_ID,
      secretAccessKey: env.AWS_SECRET_ACCESS_KEY,
    },
  });
}
