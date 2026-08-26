import Image from "next/image";

import { cn } from "~/lib/utils";

type BrandLogoProps = {
  className?: string;
  priority?: boolean;
};

export function BrandLogo({ className, priority = false }: BrandLogoProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-md bg-black px-3 py-2",
        className,
      )}
    >
      <Image
        src="/branding/lunartech-logo.png"
        alt="LUNARTECH"
        width={4511}
        height={964}
        priority={priority}
        className="h-auto w-full"
      />
    </span>
  );
}
