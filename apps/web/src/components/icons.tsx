/**
 * The dashboard's icon set: authored 16px SVGs in one 1.5px stroke.
 *
 * Kept deliberately small. Each icon names a review-tool concept (a verdict, a
 * check, a stop line) rather than decorating a heading.
 */

import type { ReactNode, SVGProps } from "react";

type IconProps = Omit<SVGProps<SVGSVGElement>, "children"> & { title?: string };

function Icon({ title, children, ...props }: IconProps & { children: ReactNode }): JSX.Element {
  return (
    <svg
      width={16}
      height={16}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden={title ? undefined : true}
      role={title ? "img" : undefined}
      className="icon"
      {...props}
    >
      {title ? <title>{title}</title> : null}
      {children}
    </svg>
  );
}

export const CheckIcon = (props: IconProps) => (
  <Icon {...props}>
    <path d="M3.5 8.5 6.5 11.5 12.5 4.5" />
  </Icon>
);

export const CrossIcon = (props: IconProps) => (
  <Icon {...props}>
    <path d="M4.5 4.5 11.5 11.5M11.5 4.5 4.5 11.5" />
  </Icon>
);

export const MinusIcon = (props: IconProps) => (
  <Icon {...props}>
    <path d="M4 8h8" />
  </Icon>
);

export const ClockIcon = (props: IconProps) => (
  <Icon {...props}>
    <circle cx="8" cy="8" r="5.5" />
    <path d="M8 5v3l2 1.5" />
  </Icon>
);

export const StopIcon = (props: IconProps) => (
  <Icon {...props}>
    <path d="M5.5 2.5h5l3 3v5l-3 3h-5l-3-3v-5z" />
    <path d="M5.5 8h5" />
  </Icon>
);

export const AlertIcon = (props: IconProps) => (
  <Icon {...props}>
    <path d="M8 2.5 14 13H2z" />
    <path d="M8 6.5v3M8 11.25v.01" />
  </Icon>
);

export const InfoIcon = (props: IconProps) => (
  <Icon {...props}>
    <circle cx="8" cy="8" r="5.5" />
    <path d="M8 7.25v3.5M8 5.25v.01" />
  </Icon>
);

export const DownloadIcon = (props: IconProps) => (
  <Icon {...props}>
    <path d="M8 2.5v8M4.75 7.5 8 10.75 11.25 7.5M3 13.5h10" />
  </Icon>
);

export const CommitIcon = (props: IconProps) => (
  <Icon {...props}>
    <circle cx="8" cy="8" r="2.5" />
    <path d="M1.5 8h4M10.5 8h4" />
  </Icon>
);

export const FileIcon = (props: IconProps) => (
  <Icon {...props}>
    <path d="M4 1.75h5l3 3v9.5H4z" />
    <path d="M9 1.75v3h3" />
  </Icon>
);

export const ShieldIcon = (props: IconProps) => (
  <Icon {...props}>
    <path d="M8 1.75 13 3.5v4c0 3-2.2 5.2-5 6.75C5.2 12.7 3 10.5 3 7.5v-4z" />
  </Icon>
);

export const PlusIcon = (props: IconProps) => (
  <Icon {...props}>
    <path d="M8 3.5v9M3.5 8h9" />
  </Icon>
);

export const ArrowRightIcon = (props: IconProps) => (
  <Icon {...props}>
    <path d="M3 8h9.5M9 4.5 12.5 8 9 11.5" />
  </Icon>
);
