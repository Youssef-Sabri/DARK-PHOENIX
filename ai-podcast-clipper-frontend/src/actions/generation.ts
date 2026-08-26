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
    credentials: {
      accessKeyId: env.AWS_ACCESS_KEY_ID,
      secretAccessKey: env.AWS_SECRET_ACCESS_KEY,
    },
  });
}
