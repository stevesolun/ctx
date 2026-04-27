---
version: alpha
name: Kraft Paper
description: Brown paper, stamp ink, handmade feel.
colors:
  primary: "#251812"
  secondary: "#8B6E52"
  tertiary: "#B83B2E"
  neutral: "#E6D6B8"
  surface: "#F1E3C6"
  on-primary: "#F1E3C6"
typography:
  display:
    fontFamily: Archivo
    fontSize: 4rem
    fontWeight: 800
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Archivo
    fontSize: 2.25rem
    fontWeight: 800
  body:
    fontFamily: Inter
    fontSize: 1rem
    lineHeight: 1.6
  label:
    fontFamily: JetBrains Mono
    fontSize: 0.72rem
    letterSpacing: "0.1em"
rounded:
  sm: 2px
  md: 4px
  lg: 8px
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

A crafts-and-goods palette. Kraft brown surfaces, deep ink primary, stamp-red accent. Feels tactile even in pixels.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#251812`):** Headlines and core text.
- **Secondary (`#8B6E52`):** Borders, captions, and metadata.
- **Tertiary (`#B83B2E`):** The sole driver for interaction. Reserve it.
- **Neutral (`#E6D6B8`):** The page foundation.

## Typography

- **display:** Archivo 4rem
- **h1:** Archivo 2.25rem
- **body:** Inter 1rem
- **label:** JetBrains Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
