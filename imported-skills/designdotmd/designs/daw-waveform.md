---
version: alpha
name: DAW Waveform
description: Audio-production UI: waveform teal, signal red, rack black.
colors:
  primary: "#E5E9EC"
  secondary: "#6F7C88"
  tertiary: "#3DD6B5"
  neutral: "#0A0E12"
  surface: "#11161C"
  on-primary: "#0A0E12"
typography:
  display:
    fontFamily: Space Grotesk
    fontSize: 3.5rem
    fontWeight: 600
    letterSpacing: "-0.025em"
  h1:
    fontFamily: Space Grotesk
    fontSize: 1.85rem
    fontWeight: 600
  body:
    fontFamily: Inter
    fontSize: 0.92rem
    lineHeight: 1.55
  label:
    fontFamily: JetBrains Mono
    fontSize: 0.7rem
    letterSpacing: "0.04em"
rounded:
  sm: 3px
  md: 6px
  lg: 10px
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

A DAW-style palette for audio tools: deep rack black, waveform teal, signal-red clip warnings.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#E5E9EC`):** Headlines and core text.
- **Secondary (`#6F7C88`):** Borders, captions, and metadata.
- **Tertiary (`#3DD6B5`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0A0E12`):** The page foundation.

## Typography

- **display:** Space Grotesk 3.5rem
- **h1:** Space Grotesk 1.85rem
- **body:** Inter 0.92rem
- **label:** JetBrains Mono 0.7rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
