---
version: alpha
name: DeFi Chrome
description: Trading-floor neon: emerald gains, bloody losses.
colors:
  primary: "#F0F2F5"
  secondary: "#7A8696"
  tertiary: "#00D395"
  neutral: "#0B0E13"
  surface: "#141820"
  on-primary: "#0B0E13"
typography:
  display:
    fontFamily: Space Grotesk
    fontSize: 3.5rem
    fontWeight: 700
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Space Grotesk
    fontSize: 1.9rem
    fontWeight: 600
  body:
    fontFamily: Inter
    fontSize: 0.92rem
    lineHeight: 1.5
  label:
    fontFamily: JetBrains Mono
    fontSize: 0.72rem
    letterSpacing: "0.04em"
rounded:
  sm: 3px
  md: 6px
  lg: 12px
spacing:
  sm: 8px
  md: 16px
  lg: 32px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.md}"
    padding: 12px 20px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 24px
---
## Overview

An on-chain trading system. Tabular numerics, high-density panels.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#F0F2F5`):** Headlines and core text.
- **Secondary (`#7A8696`):** Borders, captions, and metadata.
- **Tertiary (`#00D395`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0B0E13`):** The page foundation.

## Typography

- **display:** Space Grotesk 3.5rem
- **h1:** Space Grotesk 1.9rem
- **body:** Inter 0.92rem
- **label:** JetBrains Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
