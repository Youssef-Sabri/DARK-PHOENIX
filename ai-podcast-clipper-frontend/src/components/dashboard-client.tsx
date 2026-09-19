"use client";

import { useDropzone } from "react-dropzone";
import type { Clip } from "@prisma/client";
import Link from "next/link";
import { Button } from "./ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./ui/tabs";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "./ui/card";
import { Loader2, UploadCloud, Youtube } from "lucide-react";
import { useState } from "react";
import { generateUploadUrl } from "~/actions/s3";
import { toast } from "sonner";
import { processVideo, ingestYouTubeVideo } from "~/actions/generation";
import { Input } from "./ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "./ui/table";
import { Badge } from "./ui/badge";
import { useRouter } from "next/navigation";
import { ClipDisplay } from "./clip-display";

export function DashboardClient({
  uploadedFiles,
  clips,
}: {
  uploadedFiles: {
    id: string;
    s3Key: string;
    filename: string;
    status: string;
    clipsCount: number;
    createdAt: Date;
  }[];
  clips: Clip[];
}) {
  const [files, setFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [retryingId, setRetryingId] = useState<string | null>(null);
  const [youtubeUrl, setYoutubeUrl] = useState("");
  const [ingestingYoutube, setIngestingYoutube] = useState(false);
  const router = useRouter();

  const handleYouTubeSubmit = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    const url = youtubeUrl.trim();
    if (!url) return;

    setIngestingYoutube(true);
    try {
      const res = await ingestYouTubeVideo(url);
      if (res.success) {
        toast.success("YouTube video queued for processing!", {
          description: `Video ID: ${res.videoId}. Cloud backend will ingest and generate clips.`,
          duration: 5000,
        });
        setYoutubeUrl("");
        router.refresh();
      }
    } catch (error) {
      toast.error("Failed to ingest YouTube video", {
        description: error instanceof Error ? error.message : "Something went wrong.",
      });
    } finally {
      setIngestingYoutube(false);
    }
  };

  const handleRefresh = async () => {
    setRefreshing(true);
    router.refresh();
    setTimeout(() => setRefreshing(false), 600);
  };

  const handleDrop = (acceptedFiles: File[]) => {
    setFiles(acceptedFiles);
  };

  const handleRetry = async (uploadedFileId: string) => {
    setRetryingId(uploadedFileId);
    try {
      await processVideo(uploadedFileId);
      toast.success("Video queued again");
      router.refresh();
    } catch (error) {
      console.error("Video retry failed", error);
      toast.error("Could not retry video processing");
    } finally {
      setRetryingId(null);
    }
  };

  const { getInputProps, getRootProps, isDragActive } = useDropzone({
    onDrop: handleDrop,
    accept: { "video/mp4": [".mp4"] },
    maxSize: 500 * 1024 * 1024,
    disabled: uploading,
    maxFiles: 1,
    multiple: false,
  });

  const handleUpload = async () => {
    if (files.length === 0) return;

    const file = files[0]!;
    const contentType = file.type || "video/mp4";
    setUploading(true);

    try {
      const { success, signedUrl, uploadedFileId } = await generateUploadUrl({
        filename: file.name,
        contentType,
      });

      if (!success) throw new Error("Failed to get upload URL");

      const uploadResponse = await fetch(signedUrl, {
        method: "PUT",
        body: file,
        headers: {
          "Content-Type": contentType,
        },
      });

      if (!uploadResponse.ok)
        throw new Error(`Upload failed with status: ${uploadResponse.status}`);

      await processVideo(uploadedFileId);

      setFiles([]);

      toast.success("Video uploaded successfully", {
        description:
          "Your video has been scheduled for processing. Check the status below.",
        duration: 5000,
      });
    } catch (error) {
      console.error("Video upload failed", error);
      toast.error("Upload failed", {
        description:
          "There was a problem uploading your video. Please try again.",
      });
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="mx-auto flex max-w-5xl flex-col space-y-6 px-4 py-8">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            LUNARTECH Clipper
          </h1>
          <p className="text-muted-foreground">
            Upload your podcast and get AI-generated clips instantly
          </p>
        </div>
        <Link href="/dashboard/billing">
          <Button>Buy Credits</Button>
        </Link>
      </div>

      <Tabs defaultValue="upload">
        <TabsList>
          <TabsTrigger value="upload">Upload</TabsTrigger>
          <TabsTrigger value="my-clips">My Clips</TabsTrigger>
        </TabsList>

        <TabsContent value="upload">
          <Card>
            <CardHeader>
              <CardTitle>Upload Podcast</CardTitle>
              <CardDescription>
                Upload your audio or video file to generate clips
              </CardDescription>
            </CardHeader>
            <CardContent>
              <Tabs defaultValue="file" className="w-full">
                <TabsList className="mb-4 grid w-full max-w-xs grid-cols-2">
                  <TabsTrigger value="file">Local MP4</TabsTrigger>
                  <TabsTrigger value="youtube" className="flex items-center gap-1.5">
                    <Youtube className="h-4 w-4 text-red-500" />
                    YouTube URL
                  </TabsTrigger>
                </TabsList>

                <TabsContent value="file" className="space-y-4">
                  <div
                    {...getRootProps()}
                    className="hover:bg-muted/50 cursor-pointer rounded-lg border border-dashed transition-colors"
                  >
                    <input {...getInputProps()} />
                    <div className="flex flex-col items-center justify-center space-y-4 rounded-lg p-10 text-center">
                      <UploadCloud className="text-muted-foreground h-12 w-12" />
                      <p className="font-medium">
                        {isDragActive
                          ? "Drop your video here"
                          : "Drag and drop your file"}
                      </p>
                      <p className="text-muted-foreground text-sm">
                        or click to browse (MP4 up to 500MB)
                      </p>
                      <Button
                        type="button"
                        className="pointer-events-none"
                        variant="default"
                        size="sm"
                        disabled={uploading}
                      >
                        Select File
                      </Button>
                    </div>
                  </div>

                  <div className="flex items-start justify-between">
                    <div>
                      {files.length > 0 && (
                        <div className="space-y-1 text-sm">
                          <p className="font-medium">Selected file:</p>
                          {files.map((file) => (
                            <p key={file.name} className="text-muted-foreground">
                              {file.name}
                            </p>
                          ))}
                        </div>
                      )}
                    </div>
                    <Button
                      disabled={files.length === 0 || uploading}
                      onClick={handleUpload}
                    >
                      {uploading ? (
                        <>
                          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                          Uploading...
                        </>
                      ) : (
                        "Upload and Generate Clips"
                      )}
                    </Button>
                  </div>
                </TabsContent>

                <TabsContent value="youtube" className="space-y-4">
                  <div className="rounded-lg border p-6 space-y-4 bg-muted/20">
                    <div className="space-y-2">
                      <label className="text-sm font-medium">YouTube Video URL</label>
                      <div className="flex gap-2">
                        <Input
                          type="url"
                          placeholder="https://www.youtube.com/watch?v=YRvf00NooN8"
                          value={youtubeUrl}
                          onChange={(e) => setYoutubeUrl(e.target.value)}
                          disabled={ingestingYoutube}
                          className="flex-1"
                        />
                        <Button
                          type="button"
                          disabled={!youtubeUrl.trim() || ingestingYoutube}
                          onClick={() => handleYouTubeSubmit()}
                        >
                          {ingestingYoutube ? (
                            <>
                              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                              Ingesting...
                            </>
                          ) : (
                            "Ingest & Clip"
                          )}
                        </Button>
                      </div>
                      <p className="text-muted-foreground text-xs">
                        The video is downloaded server-side in the cloud. No local storage or bandwidth is used.
                      </p>
                    </div>

                    <div className="pt-2 border-t flex items-center justify-between">
                      <span className="text-muted-foreground text-xs">Assigned evaluation video:</span>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="text-xs"
                        onClick={() => setYoutubeUrl("https://www.youtube.com/watch?v=YRvf00NooN8")}
                      >
                        Paste Assigned URL (YRvf00NooN8)
                      </Button>
                    </div>
                  </div>
                </TabsContent>
              </Tabs>

              {uploadedFiles.length > 0 && (
                <div className="pt-6">
                  <div className="mb-2 flex items-center justify-between">
                    <h3 className="text-md mb-2 font-medium">Queue status</h3>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={handleRefresh}
                      disabled={refreshing}
                    >
                      {refreshing && (
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      )}
                      Refresh
                    </Button>
                  </div>
                  <div className="max-h-[300px] overflow-auto rounded-md border">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>File</TableHead>
                          <TableHead>Uploaded</TableHead>
                          <TableHead>Status</TableHead>
                          <TableHead>Clips created</TableHead>
                          <TableHead>Action</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {uploadedFiles.map((item) => (
                          <TableRow key={item.id}>
                            <TableCell className="max-w-xs truncate font-medium">
                              {item.filename}
                            </TableCell>
                            <TableCell className="text-muted-foreground text-sm">
                              {new Date(item.createdAt).toLocaleDateString()}
                            </TableCell>
                            <TableCell>
                              {item.status === "queued" && (
                                <Badge variant="outline">Queued</Badge>
                              )}
                              {item.status === "processing" && (
                                <Badge variant="outline">Processing</Badge>
                              )}
                              {item.status === "processed" && (
                                <Badge variant="outline">Processed</Badge>
                              )}
                              {item.status === "no credits" && (
                                <Badge variant="destructive">No credits</Badge>
                              )}
                              {item.status === "failed" && (
                                <Badge variant="destructive">Failed</Badge>
                              )}
                            </TableCell>
                            <TableCell>
                              {item.clipsCount > 0 ? (
                                <span>
                                  {item.clipsCount} clip
                                  {item.clipsCount !== 1 ? "s" : ""}
                                </span>
                              ) : (
                                <span className="text-muted-foreground">
                                  No clips yet
                                </span>
                              )}
                            </TableCell>
                            <TableCell>
                              {(item.status === "failed" ||
                                item.status === "no credits") && (
                                <Button
                                  variant="outline"
                                  size="sm"
                                  disabled={retryingId === item.id}
                                  onClick={() => handleRetry(item.id)}
                                >
                                  {retryingId === item.id && (
                                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                                  )}
                                  Retry
                                </Button>
                              )}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="my-clips">
          <Card>
            <CardHeader>
              <CardTitle>My Clips</CardTitle>
              <CardDescription>
                View and manage your generated clips here. Processing may take a
                few minutes.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <ClipDisplay clips={clips} />
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
