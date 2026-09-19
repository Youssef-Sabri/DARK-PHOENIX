import { env } from "~/env";
import { inngest } from "./client";
import { db } from "~/server/db";
import { ListObjectsV2Command, S3Client } from "@aws-sdk/client-s3";

export const processVideo = inngest.createFunction(
  {
    id: "process-video",
    retries: 1,
    concurrency: {
      limit: 1,
      key: "event.data.userId",
    },
  },
  { event: "process-video-events" },
  async ({ event, step }) => {
    const { uploadedFileId } = event.data as {
      uploadedFileId: string;
      userId: string;
    };

    try {
      const { userId, credits, s3Key } = await step.run(
        "check-credits",
        async () => {
          const uploadedFile = await db.uploadedFile.findUniqueOrThrow({
            where: {
              id: uploadedFileId,
            },
            select: {
              user: {
                select: {
                  id: true,
                  credits: true,
                },
              },
              s3Key: true,
            },
          });

          return {
            userId: uploadedFile.user.id,
            credits: uploadedFile.user.credits,
            s3Key: uploadedFile.s3Key,
          };
        },
      );

      if (credits > 0) {
        await step.run("set-status-processing", async () => {
          await db.uploadedFile.update({
            where: {
              id: uploadedFileId,
            },
            data: {
              status: "processing",
            },
          });
        });

        let processError = "Video processing failed";
        let processedSuccessfully = false;

        for (let attempt = 1; attempt <= 3; attempt += 1) {
          const processResponse = await step.fetch(env.PROCESS_VIDEO_ENDPOINT, {
            method: "POST",
            body: JSON.stringify({
              s3_key: s3Key,
              max_clips: Math.min(credits, 5),
              youtube_url: (event.data as Record<string, unknown>).youtubeUrl ?? undefined,
            }),
            headers: {
              "Content-Type": "application/json",
              Authorization: `Bearer ${env.PROCESS_VIDEO_ENDPOINT_AUTH}`,
              "X-Dark-Phoenix-Attempt": String(attempt),
            },
          });

          if (processResponse.ok) {
            processedSuccessfully = true;
            break;
          }

          const responseBody = (await processResponse.text()).slice(0, 500);
          processError = `Processing endpoint returned ${processResponse.status}: ${responseBody}`;
          const retryable =
            processResponse.status === 408 ||
            processResponse.status === 429 ||
            processResponse.status >= 500;

          if (!retryable || attempt === 3) break;

          await step.sleep(
            `wait-before-processing-retry-${attempt}`,
            `${attempt * 15}s`,
          );
        }

        if (!processedSuccessfully) {
          throw new Error(processError);
        }

        await step.run("create-clips-and-deduct-credits", async () => {
          const folderPrefix = s3Key.slice(0, s3Key.lastIndexOf("/") + 1);

          const allKeys = await listS3ObjectsByPrefix(folderPrefix);

          const clipKeys = allKeys.filter(
            (key): key is string =>
              key !== undefined && /(^|\/)clip_\d+\.mp4$/i.test(key),
          );

          await db.$transaction(async (tx) => {
            const existingClips = await tx.clip.findMany({
              where: {
                uploadedFileId,
                s3Key: { in: clipKeys },
              },
              select: { s3Key: true },
            });
            const existingKeys = new Set(
              existingClips.map((clip) => clip.s3Key),
            );
            const newKeys = clipKeys.filter((key) => !existingKeys.has(key));

            const currentUser = await tx.user.findUniqueOrThrow({
              where: { id: userId },
              select: { credits: true },
            });
            const keysToAdd = newKeys.slice(0, currentUser.credits);
            if (keysToAdd.length === 0) return;

            await tx.clip.createMany({
              data: keysToAdd.map((clipKey) => ({
                s3Key: clipKey,
                uploadedFileId,
                userId,
              })),
            });

            await tx.user.update({
              where: { id: userId },
              data: {
                credits: {
                  decrement: keysToAdd.length,
                },
              },
            });
          });
        });

        await step.run("set-status-processed", async () => {
          await db.uploadedFile.update({
            where: {
              id: uploadedFileId,
            },
            data: {
              status: "processed",
            },
          });
        });
      } else {
        await step.run("set-status-no-credits", async () => {
          await db.uploadedFile.update({
            where: {
              id: uploadedFileId,
            },
            data: {
              status: "no credits",
            },
          });
        });
      }
    } catch (error: unknown) {
      await db.uploadedFile.update({
        where: {
          id: uploadedFileId,
        },
        data: {
          status: "failed",
        },
      });

      console.error("Video processing workflow failed", error);
      throw error instanceof Error
        ? error
        : new Error("Unknown video processing failure");
    }
  },
);

async function listS3ObjectsByPrefix(prefix: string) {
  const s3Client = new S3Client({
    region: env.AWS_REGION,
    endpoint: env.AWS_ENDPOINT_URL_S3,
    forcePathStyle: Boolean(env.AWS_ENDPOINT_URL_S3),
    credentials: {
      accessKeyId: env.AWS_ACCESS_KEY_ID,
      secretAccessKey: env.AWS_SECRET_ACCESS_KEY,
    },
  });

  const keys: string[] = [];
  let continuationToken: string | undefined;

  do {
    const response = await s3Client.send(
      new ListObjectsV2Command({
        Bucket: env.S3_BUCKET_NAME,
        Prefix: prefix,
        ContinuationToken: continuationToken,
      }),
    );
    keys.push(
      ...(response.Contents?.map((item) => item.Key).filter(
        (key): key is string => Boolean(key),
      ) ?? []),
    );
    continuationToken = response.IsTruncated
      ? response.NextContinuationToken
      : undefined;
  } while (continuationToken);

  return keys;
}
