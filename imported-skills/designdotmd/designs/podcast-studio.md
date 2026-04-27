---
version: alpha
name: Podcast Studio
description: Late-night show: mic amber, felt maroon, velvet curtain.
colors:
  primary: "#F3E6D0"
  secondary: "#A89078"
  tertiary: "#F2A541"
  neutral: "#3A1A1E"
  surface: "#4A2328"
  on-primary: "#3A1A1E"
typography:
  display:
    fontFamily: Fraunces
    fontSize: 4.5rem
    fontWeight: 500
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Fraunces
    fontSize: 2.3rem
    fontWeight: 500
  body:
    fontFamily: Inter
    fontSize: 0.98rem
    lineHeight: 1.65
  label:
    fontFamily: JetBrains Mono
    fontSize: 0.72rem
    letterSpacing: "0.08em"
rounded:
  sm: 6px
  md: 12px
  lg: 20px
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

A podcast/audio-show system with late-night warmth: maroon surface, amber mic accent, serif titles.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#F3E6D0`):** Headlines and core text.
- **Secondary (`#A89078`):** Borders, captions, and metadata.
- **Tertiary (`#F2A541`):** The sole driver for interaction. Reserve it.
- **Neutral (`#3A1A1E`):** The page foundation.

## Typography

- **display:** Fraunces 4.5rem
- **h1:** Fraunces 2.3rem
- **body:** Inter 0.98rem
- **label:** JetBrains Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
