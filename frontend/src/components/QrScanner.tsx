import { useEffect, useRef, useState } from "react";
import { Camera, ImageUp } from "lucide-react";
import { Button } from "./ui";

/*
 * Reads a QR code from the camera (browsers allow camera access only on HTTPS or localhost) or
 * from a photo, which works everywhere, including plain-HTTP installs. Decoding happens in the
 * browser; jsQR is downloaded only when this opens.
 */

type Decoder = (img: ImageData) => string | null;

let decoderPromise: Promise<Decoder> | null = null;
function loadDecoder(): Promise<Decoder> {
  decoderPromise ??= import("jsqr").then(
    ({ default: jsQR }) =>
      (img: ImageData) =>
        jsQR(img.data, img.width, img.height, { inversionAttempts: "attemptBoth" })?.data ?? null,
  );
  return decoderPromise;
}

function pixels(source: CanvasImageSource, w: number, h: number, canvas: HTMLCanvasElement): ImageData {
  const scale = Math.min(1, 1024 / Math.max(w, h));
  canvas.width = Math.round(w * scale);
  canvas.height = Math.round(h * scale);
  const ctx = canvas.getContext("2d", { willReadFrequently: true })!;
  ctx.drawImage(source, 0, 0, canvas.width, canvas.height);
  return ctx.getImageData(0, 0, canvas.width, canvas.height);
}

export function QrScanner({ onResult }: { onResult: (text: string) => void }) {
  const cameraPossible = window.isSecureContext && !!navigator.mediaDevices?.getUserMedia;
  const [wantCamera, setWantCamera] = useState(false);
  const [camera, setCamera] = useState<"off" | "starting" | "on" | "error">("off");
  const [cameraError, setCameraError] = useState("");
  const [fileError, setFileError] = useState("");
  const video = useRef<HTMLVideoElement>(null);
  const canvas = useRef<HTMLCanvasElement | null>(null);
  const done = useRef(false);
  const result = useRef(onResult);
  result.current = onResult;

  const finish = (text: string) => {
    if (done.current) return;
    done.current = true;
    result.current(text);
  };

  useEffect(() => {
    if (!wantCamera) return;
    setCamera("starting");
    let stream: MediaStream | null = null;
    let timer = 0;
    let stopped = false;
    canvas.current ??= document.createElement("canvas");
    (async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: { ideal: "environment" } },
          audio: false,
        });
        if (stopped) return;
        const v = video.current!;
        v.srcObject = stream;
        await v.play();
        setCamera("on");
        const decode = await loadDecoder();
        const tick = () => {
          if (stopped || done.current) return;
          if (v.readyState >= 2 && v.videoWidth) {
            const text = decode(pixels(v, v.videoWidth, v.videoHeight, canvas.current!));
            if (text) return finish(text);
          }
          timer = window.setTimeout(tick, 250);
        };
        tick();
      } catch (e) {
        if (stopped) return;
        const name = e instanceof DOMException ? e.name : "";
        setCameraError(
          name === "NotAllowedError"
            ? "Camera access was blocked. Allow it in the browser's site settings, or use a photo instead."
            : name === "NotFoundError"
              ? "No camera was found. Use a photo instead."
              : "The camera could not be started. Use a photo instead.",
        );
        setCamera("error");
        setWantCamera(false);
      }
    })();
    return () => {
      stopped = true;
      clearTimeout(timer);
      stream?.getTracks().forEach((t) => t.stop());
    };
  }, [wantCamera]);

  const fromFile = async (file: File | undefined) => {
    if (!file) return;
    setFileError("");
    try {
      const [bitmap, decode] = await Promise.all([createImageBitmap(file), loadDecoder()]);
      canvas.current ??= document.createElement("canvas");
      const text = decode(pixels(bitmap, bitmap.width, bitmap.height, canvas.current));
      bitmap.close();
      if (text) finish(text);
      else setFileError("No QR code was found in that image. Try a closer, sharper photo.");
    } catch {
      setFileError("That file could not be read as an image.");
    }
  };

  return (
    <div className="space-y-3">
      {(camera === "starting" || camera === "on") && (
        <div className="relative overflow-hidden rounded-xl bg-black">
          <video ref={video} muted playsInline className="aspect-square w-full object-cover" aria-label="Camera preview" />
          <div aria-hidden className="pointer-events-none absolute inset-[18%] rounded-2xl border-2 border-white/80 shadow-[0_0_0_999px_rgba(0,0,0,0.35)]" />
          <p className="absolute inset-x-0 bottom-2 text-center text-xs text-white/90" aria-live="polite">
            {camera === "starting" ? "Starting camera…" : "Point the camera at a MeshCore channel QR code"}
          </p>
        </div>
      )}
      {camera === "off" && cameraPossible && !wantCamera && (
        <Button variant="primary" className="w-full" onClick={() => setWantCamera(true)}>
          <Camera className="size-4" aria-hidden /> Use camera
        </Button>
      )}
      {camera === "error" && (
        <p role="alert" className="text-sm text-danger">
          {cameraError}
        </p>
      )}
      <label className="flex min-h-11 w-full cursor-pointer items-center justify-center gap-2 rounded-lg border border-line bg-surface px-4 text-sm font-medium hover:bg-surface-2 focus-within:border-accent">
        <ImageUp className="size-4" aria-hidden />
        {cameraPossible ? "Choose a photo instead" : "Take or choose a photo"}
        <input
          type="file"
          accept="image/*"
          className="sr-only"
          onChange={(e) => {
            void fromFile(e.target.files?.[0]);
            e.target.value = "";
          }}
        />
      </label>
      {fileError && (
        <p role="alert" className="text-sm text-danger">
          {fileError}
        </p>
      )}
    </div>
  );
}
